/* WebUI-backed password verification for the LibreEcho Dropbear build. */

#include "includes.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define LE_USERS_PATH "/data/libreecho/config/users"
#define LE_MAX_USERS 8
#define LE_USERNAME_MAX 32
#define LE_SALT_MAX 65
#define LE_DIGEST_MAX 65
#define LE_PASSWORD_MAX 128
#define LE_LINE_MAX 256

struct le_user_record {
	char username[LE_USERNAME_MAX];
	char salt[LE_SALT_MAX];
	char digest[LE_DIGEST_MAX];
};

struct le_sha256 {
	uint32_t h[8];
	uint64_t bits;
	unsigned char block[64];
	size_t used;
};

static const uint32_t le_sha256_k[64] = {
	0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
	0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
	0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
	0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
	0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
	0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
	0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
	0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
	0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
	0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
	0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
	0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
	0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
	0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
	0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
	0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
};

static uint32_t le_rotr(uint32_t value, unsigned int count)
{
	return (value >> count) | (value << (32U - count));
}

static void le_sha256_block(struct le_sha256 *ctx)
{
	uint32_t w[64];
	uint32_t a, b, c, d, e, f, g, h;
	unsigned int i;

	for (i = 0; i < 16; ++i) {
		unsigned int offset = i * 4U;
		w[i] = ((uint32_t)ctx->block[offset] << 24) |
			((uint32_t)ctx->block[offset + 1] << 16) |
			((uint32_t)ctx->block[offset + 2] << 8) |
			(uint32_t)ctx->block[offset + 3];
	}
	for (i = 16; i < 64; ++i) {
		uint32_t s0 = le_rotr(w[i - 15], 7) ^ le_rotr(w[i - 15], 18) ^
			(w[i - 15] >> 3);
		uint32_t s1 = le_rotr(w[i - 2], 17) ^ le_rotr(w[i - 2], 19) ^
			(w[i - 2] >> 10);
		w[i] = w[i - 16] + s0 + w[i - 7] + s1;
	}

	a = ctx->h[0]; b = ctx->h[1]; c = ctx->h[2]; d = ctx->h[3];
	e = ctx->h[4]; f = ctx->h[5]; g = ctx->h[6]; h = ctx->h[7];
	for (i = 0; i < 64; ++i) {
		uint32_t s1 = le_rotr(e, 6) ^ le_rotr(e, 11) ^ le_rotr(e, 25);
		uint32_t ch = (e & f) ^ ((~e) & g);
		uint32_t temp1 = h + s1 + ch + le_sha256_k[i] + w[i];
		uint32_t s0 = le_rotr(a, 2) ^ le_rotr(a, 13) ^ le_rotr(a, 22);
		uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
		uint32_t temp2 = s0 + maj;
		h = g; g = f; f = e; e = d + temp1;
		d = c; c = b; b = a; a = temp1 + temp2;
	}
	ctx->h[0] += a; ctx->h[1] += b; ctx->h[2] += c; ctx->h[3] += d;
	ctx->h[4] += e; ctx->h[5] += f; ctx->h[6] += g; ctx->h[7] += h;
}

static void le_sha256_init(struct le_sha256 *ctx)
{
	static const uint32_t initial[8] = {
		0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
		0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
	};
	memcpy(ctx->h, initial, sizeof(initial));
	ctx->bits = 0;
	ctx->used = 0;
}

static void le_sha256_update(struct le_sha256 *ctx, const void *data, size_t size)
{
	const unsigned char *bytes = (const unsigned char *)data;
	ctx->bits += (uint64_t)size * 8U;
	while (size) {
		size_t chunk = sizeof(ctx->block) - ctx->used;
		if (chunk > size)
			chunk = size;
		memcpy(ctx->block + ctx->used, bytes, chunk);
		ctx->used += chunk;
		bytes += chunk;
		size -= chunk;
		if (ctx->used == sizeof(ctx->block)) {
			le_sha256_block(ctx);
			ctx->used = 0;
		}
	}
}

static void le_sha256_final(struct le_sha256 *ctx, unsigned char digest[32])
{
	unsigned int i;
	uint64_t bits = ctx->bits;

	ctx->block[ctx->used++] = 0x80;
	if (ctx->used > 56) {
		while (ctx->used < sizeof(ctx->block))
			ctx->block[ctx->used++] = 0;
		le_sha256_block(ctx);
		ctx->used = 0;
	}
	while (ctx->used < 56)
		ctx->block[ctx->used++] = 0;
	for (i = 0; i < 8; ++i)
		ctx->block[56 + i] = (unsigned char)(bits >> (56 - i * 8));
	le_sha256_block(ctx);
	for (i = 0; i < 8; ++i) {
		digest[i * 4] = (unsigned char)(ctx->h[i] >> 24);
		digest[i * 4 + 1] = (unsigned char)(ctx->h[i] >> 16);
		digest[i * 4 + 2] = (unsigned char)(ctx->h[i] >> 8);
		digest[i * 4 + 3] = (unsigned char)ctx->h[i];
	}
}

static int le_hex(unsigned char value)
{
	return (value >= '0' && value <= '9') ||
		(value >= 'a' && value <= 'f') ||
		(value >= 'A' && value <= 'F');
}

static int le_safe_username(const char *value)
{
	size_t i, length = strlen(value);
	if (!length || length >= LE_USERNAME_MAX)
		return 0;
	for (i = 0; i < length; ++i) {
		unsigned char c = (unsigned char)value[i];
		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
			(c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-'))
			return 0;
	}
	return 1;
}

static void le_fold_username(char out[LE_USERNAME_MAX], const char *value)
{
	size_t i;
	for (i = 0; i + 1 < LE_USERNAME_MAX && value[i]; ++i) {
		unsigned char c = (unsigned char)value[i];
		out[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : (char)c;
	}
	out[i] = '\0';
}

static int le_constant_equal(const char *a, const char *b, size_t length)
{
	unsigned int different = 0;
	size_t i;
	for (i = 0; i < length; ++i)
		different |= (unsigned char)a[i] ^ (unsigned char)b[i];
	return different == 0;
}

static int le_parse_users(struct le_user_record users[LE_MAX_USERS], size_t *count)
{
	struct stat st;
	FILE *file;
	char line[LE_LINE_MAX];
	size_t users_count = 0;

	if (lstat(LE_USERS_PATH, &st) < 0 || !S_ISREG(st.st_mode) ||
		(st.st_mode & 077) || st.st_uid != 0)
		return -1;
	file = fopen(LE_USERS_PATH, "r");
	if (!file)
		return -1;
	while (fgets(line, sizeof(line), file)) {
		char *fields[4], *cursor, *end;
		char folded[LE_USERNAME_MAX];
		size_t field_count = 0, i;

		if (!strchr(line, '\n') && !feof(file))
			goto invalid;
		end = strchr(line, '\n');
		if (end)
			*end = '\0';
		cursor = line;
		while (*cursor == ' ' || *cursor == '\t')
			++cursor;
		if (!*cursor || *cursor == '#')
			continue;
		fields[field_count++] = cursor;
		while (field_count < 4 && (end = strchr(cursor, ':')) != NULL) {
			*end = '\0';
			cursor = end + 1;
			fields[field_count++] = cursor;
		}
		if (field_count != 4 || strchr(fields[3], ':') ||
			!le_safe_username(fields[0]) || strcmp(fields[1], "sha256") != 0 ||
			strlen(fields[2]) < 16 || strlen(fields[2]) >= LE_SALT_MAX ||
			strlen(fields[3]) != 64)
			goto invalid;
		for (i = 0; i < strlen(fields[2]); ++i)
			if (!le_hex((unsigned char)fields[2][i]))
				goto invalid;
		for (i = 0; i < 64; ++i)
			if (!le_hex((unsigned char)fields[3][i]))
				goto invalid;
		le_fold_username(folded, fields[0]);
		if (strcmp(folded, "root") == 0)
			goto invalid;
		for (i = 0; i < users_count; ++i)
			if (strcmp(users[i].username, folded) == 0)
				goto invalid;
		if (users_count == LE_MAX_USERS)
			goto invalid;
		strncpy(users[users_count].username, folded, LE_USERNAME_MAX - 1);
		strncpy(users[users_count].salt, fields[2], LE_SALT_MAX - 1);
		strncpy(users[users_count].digest, fields[3], LE_DIGEST_MAX - 1);
		++users_count;
	}
	if (ferror(file) || users_count == 0)
		goto invalid;
	fclose(file);
	*count = users_count;
	return 0;

invalid:
	fclose(file);
	return -1;
}

int libreecho_auth_password(const char *username, const char *password,
		unsigned int password_length)
{
	struct le_user_record users[LE_MAX_USERS];
	char folded[LE_USERNAME_MAX];
	size_t count, i;

	if (!username || !password || password_length > LE_PASSWORD_MAX ||
		!le_safe_username(username) || le_parse_users(users, &count) < 0)
		return 0;
	le_fold_username(folded, username);
	for (i = 0; i < count; ++i) {
		struct le_sha256 ctx;
		unsigned char digest[32];
		char digest_hex[65];
		static const char hex[] = "0123456789abcdef";
		unsigned int j;

		if (strcmp(users[i].username, folded) != 0)
			continue;
		le_sha256_init(&ctx);
		le_sha256_update(&ctx, users[i].salt, strlen(users[i].salt));
		le_sha256_update(&ctx, ":", 1);
		le_sha256_update(&ctx, password, password_length);
		le_sha256_final(&ctx, digest);
		for (j = 0; j < sizeof(digest); ++j) {
			digest_hex[j * 2] = hex[digest[j] >> 4];
			digest_hex[j * 2 + 1] = hex[digest[j] & 15];
		}
		digest_hex[64] = '\0';
		return le_constant_equal(digest_hex, users[i].digest, 64);
	}
	return 0;
}
