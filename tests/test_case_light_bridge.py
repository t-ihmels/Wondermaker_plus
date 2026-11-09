"""Compile the screen bridge and exercise its actual preference forwarding."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'client-preload/case-light/wmp_case_light.c'


@pytest.fixture(scope='module')
def harness(tmp_path_factory):
    if not shutil.which('gcc'):
        pytest.skip('gcc required')
    tmp = tmp_path_factory.mktemp('case-light-bridge')
    source = tmp / 'bridge.c'
    source.write_text('#include "' + str(SOURCE) + '"\n' + r'''
#include <assert.h>
static int low_calls, high_calls, sent, destroyed;
static int wake_calls, brightness_calls, next_preference;
static unsigned char led;
static int raw_value(void) { return 0; }
static int wake(void) { wake_calls++; return next_preference; }
static void brightness(unsigned int value) {
    // The button preference must be ready before the backlight is enabled.
    if (value && next_preference >= 0) assert(led == next_preference);
    brightness_calls++;
}
static int direct_calls, direct_value;
static int no_direct(int enabled) { direct_calls++; direct_value=enabled; return 0; }
static int yes_direct(int enabled) { direct_calls++; direct_value=enabled; return 1; }
static char last_command[128];
static int low(void) { low_calls++; return 17; }
static int high(void) { high_calls++; return 23; }
static void construct(void *value) { memset(value, 0, 32); }
static void *assign(void *value, const char *text) { memcpy(value, &text, sizeof text); return value; }
static void destroy(void *value) { (void)value; destroyed++; }
static void sender(void *value) {
    const char *text; memcpy(&text, value, sizeof text);
    snprintf(last_command, sizeof last_command, "%s", text); sent++;
}
int main(int argc, char **argv) {
    if (argc == 2) {
        FILE *f = fopen(argv[1], "rb");
        if (!f) return 2;
        const struct profile *p = identify(f); fclose(f);
        if (!p) return 3;
        printf("%s %lx %lx\n", p->version, (unsigned long)p->low, (unsigned long)p->high);
        return 0;
    }
    original_low = low; original_high = high; run_gcode = sender;
    direct_preference = no_direct;
    string_construct = construct; string_assign = assign; string_destroy = destroy;
    assert(light_low() == 17);
    assert(low_calls == 1 && high_calls == 0 && sent == 1 && destroyed == 1);
    assert(!strcmp(last_command, "SET_PIN PIN=case_light VALUE=0"));
    // Repeated OFF still forwards intent, even if physical GPIO was already off.
    assert(light_low() == 17 && sent == 2);
    assert(light_high() == 23);
    assert(high_calls == 1 && sent == 3 && destroyed == 3);
    assert(!strcmp(last_command, "SET_PIN PIN=case_light VALUE=1"));
    assert(direct_calls == 3 && direct_value == 1);
    direct_preference = yes_direct;
    assert(light_low() == 17 && direct_calls == 4 && direct_value == 0);
    assert(sent == 3); // Successful direct API must not also queue G-code.
    original_get = raw_value; original_brightness = brightness;
    screen_led_on = &led; wake_policy = wake;
    atomic_store(&cached_preference, -1);
    assert(light_value() == 0); // Fallback before the controller is ready.
    atomic_store(&cached_preference, 1);
    assert(light_value() == 1); // Saved ON, even with physical LED asleep.
    atomic_store(&cached_preference, 0);
    assert(light_value() == 0);
    next_preference = 1; screen_brightness(100);
    assert(wake_calls == 1 && brightness_calls == 1 && led == 1);
    next_preference = 0; screen_brightness(50);
    assert(wake_calls == 2 && brightness_calls == 2 && led == 0);
    screen_brightness(0);
    assert(wake_calls == 2 && brightness_calls == 3); // Sleep never enables.
    next_preference = -1; led = 1; screen_brightness(100);
    assert(brightness_calls == 4 && led == 1); // Offline wake still works.
    // Multiple hooks can occupy a single page: all writes must precede RX.
    long page = sysconf(_SC_PAGESIZE);
    void *code = mmap(NULL, page, PROT_READ | PROT_WRITE | PROT_EXEC,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(code != MAP_FAILED);
    uintptr_t sites[] = {(uintptr_t)code, (uintptr_t)code + 32, (uintptr_t)code + 64};
    uintptr_t targets[] = {123, 456, 789};
    install_jumps(sites, targets, 3);
    for (int i = 0; i < 3; i++) {
        uintptr_t target; memcpy(&target, (void *)(sites[i] + 8), sizeof target);
        assert(target == targets[i]);
    }
    munmap(code, page);
    char path[] = "/tmp/wmp-light-permissions-XXXXXX";
    int fd = mkstemp(path); assert(fd >= 0);
    assert(write(fd, "1\n", 2) == 2); assert(!fchmod(fd, 0440)); close(fd);
    assert(!grant_gpio(path, getuid()));
    struct stat metadata; assert(!stat(path, &metadata));
    assert((metadata.st_mode & 0777) == 0640);
    FILE *value = fopen(path, "r"); assert(value && fgetc(value) == '1'); fclose(value);
    char link[128]; snprintf(link, sizeof link, "%s-link", path);
    assert(!symlink(path, link)); assert(!chmod(path, 0440));
    assert(grant_gpio(link, getuid()) == -1);
    assert(!stat(path, &metadata) && (metadata.st_mode & 0777) == 0440);
    unlink(link); unlink(path);
    return 0;
}
''')
    binary = tmp / 'bridge-test'
    for args in [
        ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', str(source), '-ldl', '-o', str(binary)],
        ['gcc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', str(SOURCE), '-ldl', '-o', str(tmp / 'bridge.so')],
    ]:
        subprocess.run(args, check=True, capture_output=True, text=True)
    return binary


def test_manual_choices_forward_to_saved_preference_and_preserve_vendor_setter(harness):
    subprocess.run([str(harness)], check=True)


def test_unknown_binary_is_rejected(harness):
    assert subprocess.run([str(harness), str(harness)]).returncode == 3


@pytest.mark.parametrize('version,low,high,get_value,brightness', [
    ('1.1.08', 0x624bd0, 0x624d94, 0x624f58, 0x552f8c),
    ('1.1.12', 0x624f28, 0x6250ec, 0x6252b0, 0x55307c),
    ('1.1.13', 0x624f14, 0x6250d8, 0x62529c, 0x553068),
])
def test_real_stock_setter_guards_and_position_independent_prologues(harness, tmp_path, version, low, high, get_value, brightness):
    vendor = Path(os.environ.get('WMP_VENDOR_ROOT', str(ROOT / 'analysis')))
    elf = vendor / f'fw_{version}_payload/root/home/t13dp/TM_T1/bin/client'
    if not elf.exists():
        pytest.skip('local vendor binary required')
    result = subprocess.check_output([str(harness), str(elf)], text=True)
    assert result.strip() == f'{version} {low:x} {high:x}'
    with elf.open('rb') as stream:
        for address in (low, high):
            stream.seek(address - 0x400000)
            # stp frame; mov frame; str x19; mov w1,0 -- safe in a trampoline.
            assert stream.read(16).hex() == 'fd7bbda9fd030091f30b00f901008052'
        for address, expected in [
            (get_value, 'fd7bb9a9fd030091f30b00f900008012'),
            (brightness, 'ff030bd1fd7b00a9fd030091f30b00f9'),
        ]:
            stream.seek(address - 0x400000)
            assert stream.read(16).hex() == expected
    symbols = subprocess.check_output(['nm', str(elf)], text=True)
    assert '000000000386ce2c B led_on' in symbols
    changed = tmp_path / 'modified-client'
    shutil.copyfile(elf, changed)
    with changed.open('r+b') as stream:
        stream.seek(low - 0x400000)
        stream.write(b'\0')
    assert subprocess.run([str(harness), str(changed)]).returncode == 3
