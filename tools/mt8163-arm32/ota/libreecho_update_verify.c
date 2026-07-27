// SPDX-License-Identifier: GPL-2.0-or-later
/* Verify an Ed25519 signature over an exact OTA manifest. */
#include <errno.h>
#include <sodium.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MANIFEST_MAX (64 * 1024)

static int read_file(const char *path, unsigned char **data, size_t *size,
                     size_t maximum)
{
    FILE *stream;
    long length;
    unsigned char *buffer;

    stream = fopen(path, "rb");
    if (!stream)
        return -1;
    if (fseek(stream, 0, SEEK_END) || (length = ftell(stream)) < 0 ||
        (size_t)length > maximum || fseek(stream, 0, SEEK_SET)) {
        fclose(stream);
        return -1;
    }
    buffer = malloc((size_t)length + 1);
    if (!buffer) {
        fclose(stream);
        return -1;
    }
    if (fread(buffer, 1, (size_t)length, stream) != (size_t)length) {
        free(buffer);
        fclose(stream);
        return -1;
    }
    fclose(stream);
    buffer[length] = 0;
    *data = buffer;
    *size = (size_t)length;
    return 0;
}

static int read_hex(const char *path, unsigned char *output, size_t output_size)
{
    unsigned char *text = NULL;
    size_t size = 0, index;
    int result = -1;

    if (read_file(path, &text, &size, output_size * 2 + 2))
        return -1;
    while (size && (text[size - 1] == '\n' || text[size - 1] == '\r'))
        text[--size] = 0;
    if (size == output_size * 2) {
        result = 0;
        for (index = 0; index < output_size; ++index) {
            unsigned char high = text[index * 2];
            unsigned char low = text[index * 2 + 1];
            if (high >= '0' && high <= '9')
                high -= '0';
            else if (high >= 'a' && high <= 'f')
                high = (unsigned char)(high - 'a' + 10);
            else {
                result = -1;
                break;
            }
            if (low >= '0' && low <= '9')
                low -= '0';
            else if (low >= 'a' && low <= 'f')
                low = (unsigned char)(low - 'a' + 10);
            else {
                result = -1;
                break;
            }
            output[index] = (unsigned char)((high << 4) | low);
        }
    }
    memset(text, 0, size);
    free(text);
    return result;
}

int main(int argc, char **argv)
{
    unsigned char public_key[crypto_sign_PUBLICKEYBYTES];
    unsigned char signature[crypto_sign_BYTES];
    unsigned char *manifest = NULL;
    size_t manifest_size = 0;
    int result;

    if (argc != 4) {
        fprintf(stderr, "Usage: %s PUBLIC_KEY MANIFEST SIGNATURE\n", argv[0]);
        return 2;
    }
    if (read_hex(argv[1], public_key, sizeof(public_key)) ||
        read_hex(argv[3], signature, sizeof(signature)) ||
        read_file(argv[2], &manifest, &manifest_size, MANIFEST_MAX)) {
        fprintf(stderr, "ERROR: invalid update key, signature, or manifest\n");
        return 1;
    }
    result = crypto_sign_verify_detached(signature, manifest,
                                         (unsigned long long)manifest_size,
                                         public_key);
    memset(public_key, 0, sizeof(public_key));
    memset(signature, 0, sizeof(signature));
    free(manifest);
    if (result) {
        fprintf(stderr, "ERROR: OTA manifest signature rejected\n");
        return 1;
    }
    puts("ota_manifest_signature=PASS");
    return 0;
}
