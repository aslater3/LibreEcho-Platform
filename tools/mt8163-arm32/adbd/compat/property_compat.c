#include <string.h>

int property_get(const char *name, char *value, const char *default_value)
{
    const char *result = default_value ? default_value : "";
    if (!strcmp(name, "ro.secure")) result = "0";
    else if (!strcmp(name, "ro.debuggable")) result = "1";
    else if (!strcmp(name, "ro.adb.secure")) result = "0";
    else if (!strcmp(name, "ro.kernel.qemu")) result = "0";
    else if (!strcmp(name, "service.adb.root")) result = "";
    else if (!strcmp(name, "service.adb.tcp.port")) result = "0";
    if (value) {
        strncpy(value, result, 91);
        value[91] = '\0';
    }
    return (int)strlen(result);
}

int property_set(const char *name, const char *value)
{
    (void)name;
    (void)value;
    return 0;
}
