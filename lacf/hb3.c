#include "hb3.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The two tables (hbi.c: info-native, hbc.c: comment-only). */
size_t hbi_payload_encode(const uint8_t *text, size_t tl, uint8_t *out, size_t cap);
int    hbi_payload_check(const uint8_t *pay, size_t n, size_t L, uint8_t *text);
size_t hbc_payload_encode(const uint8_t *text, size_t tl, uint8_t *out, size_t cap);
int    hbc_payload_check(const uint8_t *pay, size_t n, size_t L, uint8_t *text);

#define MODEL_VERSION 0                     /* model version 0: the two PPM tables above (the destination list is not versioned: fixed, append-only codes) */
#define VBYTE(has_cap) ((uint8_t)(((has_cap) ? 0x80 : 0) | MODEL_VERSION))   /* 3.0.0-dev.1 (proposal): V = c ss mmmmm, c = three capability bytes follow V, ss two spare bits (0), mmmmm the model version */
#define MAX_LIST 126                        /* destination codes 0 to 125; 126 is reserved and 127 is the escape */
static char list[MAX_LIST][12]; static int nlist = 0;

const char *hb3_reason(int c)
{
    switch (c) {
    case HB3_ERR_PREFIX: return "prefix"; case HB3_ERR_KIND: return "kind"; case HB3_ERR_RESERVED: return "reserved bit"; case HB3_ERR_FRONT: return "address or path";
    case HB3_ERR_RXT: return "RXT block"; case HB3_ERR_VERSION: return "version or coding"; case HB3_ERR_CONTENT: return "content structure"; case HB3_ERR_PAYLOAD: return "payload is not canonical";
    case HB3_ERR_CRC: return "CRC"; case HB3_ERR_SIZE: return "size rule"; case HB3_ERR_RANGE: return "tuple range"; case HB3_ERR_CODE_RESERVED: return "reserved destination code";
    case HB3_ERR_POSITION: return "position block"; default: return "?"; }
}

uint16_t hb3_crc16(const uint8_t *p, size_t n)
{
    uint16_t c = 0xFFFF; size_t i; int b;
    for (i = 0; i < n; i++) { c ^= p[i]; for (b = 0; b < 8; b++) c = (c & 1) ? (uint16_t)((c >> 1) ^ 0x8408) : (uint16_t)(c >> 1); }
    return (uint16_t)~c;
}

int hb3_set_list(const char *const *names, int n)
{
    int i;
    if (n < 0 || n > MAX_LIST) return -1;
    for (i = 0; i < n; i++) { if (strlen(names[i]) >= sizeof list[0]) return -1; strcpy(list[i], names[i]); }
    nlist = n;
    return n;
}

int hb3_load_list(const char *path)
{
    FILE *f = fopen(path, "r"); char line[128];
    if (!f) return -1;
    nlist = 0;
    while (fgets(line, sizeof line, f) && nlist < MAX_LIST) {
        int idx; char d[16];
        if (line[0] == '#') continue;
        if (sscanf(line, "%d %15s", &idx, d) == 2 && idx == nlist && strlen(d) < sizeof list[0]) { strcpy(list[nlist], d); nlist++; }
    }
    fclose(f);
    return nlist;
}

/* ---- address elements ---- */
static const char B40[] = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

static int match_wide(const char *e, int *n, int *N, int *star)
{
    const char *p = e; *N = 0; *star = 0;
    if (strncmp(p, "WIDE", 4)) return 0;
    p += 4;
    if (*p < '1' || *p > '7') return 0;
    *n = *p++ - '0';
    if (*p == '-') { p++; if (*p < '1' || *p > '7') return 0; *N = *p++ - '0'; if (*N > *n) return 0; }
    if (*p == '*') { *star = 1; p++; }
    return *p == 0;
}

/* A path element shaped like a WIDE alias (WIDE, digits, optional - and digits, optional *) has to follow the APRS rule: n 1 to 7, N 1 to n.
 * One that does not (WIDE1-2, WIDE2-3, WIDE8-1) is not carried by LACF, and a relay does not repeat it on RF (spec 11, "Non-compliant WIDE paths").
 * Returns 1 for anything else, including an ordinary name that merely starts with WIDE. */
static int wide_compliant(const char *e)
{
    const char *p = e; int n = 0, N = 0, nd = 0, Nd = 0, dash = 0;
    if (strncmp(p, "WIDE", 4)) return 1;
    p += 4;
    if (*p < '0' || *p > '9') return 1;
    while (*p >= '0' && *p <= '9') { if (n < 1000) n = n * 10 + (*p - '0'); nd++; p++; }
    if (*p == '-') { dash = 1; p++; if (*p < '0' || *p > '9') return 1; while (*p >= '0' && *p <= '9') { if (N < 1000) N = N * 10 + (*p - '0'); Nd++; p++; } }
    if (*p == '*') p++;
    if (*p != 0) return 1;
    if (nd != 1 || n < 1 || n > 7) return 0;
    if (dash && (Nd != 1 || N < 1 || N > n)) return 0;
    return 1;
}

static int match_call(const char *e, char *call, int *ssid, int *star)
{
    size_t k = 0; const char *p = e; *ssid = 0; *star = 0;
    while (k < 6 && ((*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9'))) call[k++] = *p++;
    if (!k) return 0;
    call[k] = 0;
    if (*p == '-') {
        long v = 0; int digits = 0; p++;
        if (*p < '1' || *p > '9') return 0;                           /* no leading zero, not "-0" */
        while (*p >= '0' && *p <= '9' && digits < 4) { v = v * 10 + (*p++ - '0'); digits++; }
        if (v < 1 || v > 255) return 0;
        *ssid = (int)v;
    }
    if (*p == '*') { *star = 1; p++; }
    return *p == 0;
}

static int enc_element(const char *e, uint8_t *out, size_t cap, size_t *w)
{
    int n, N, star, ssid; char call[8]; size_t len = strlen(e);
    if (match_wide(e, &n, &N, &star)) { if (cap < 1) return 0; out[0] = (uint8_t)((star ? 0x40 : 0) | n << 3 | N); *w = 1; return 1; }
    if (match_call(e, call, &ssid, &star)) {
        uint32_t v = 0; size_t k, need = 5 + (ssid > 30 ? 1 : 0);
        if (cap < need) return 0;
        for (k = 0; k < 6; k++) { char c = k < strlen(call) ? call[k] : ' '; v = v * 40 + (uint32_t)(strchr(B40, c) - B40); }
        out[0] = (uint8_t)(0x80 | (star ? 0x20 : 0) | (ssid <= 30 ? ssid : 31));
        out[1] = (uint8_t)(v >> 24); out[2] = (uint8_t)(v >> 16); out[3] = (uint8_t)(v >> 8); out[4] = (uint8_t)v;
        if (ssid > 30) out[5] = (uint8_t)ssid;
        *w = need; return 1;
    }
    if (len < 1 || len > 31 || cap < 1 + len) return 0;
    out[0] = (uint8_t)(0xC0 | len); memcpy(out + 1, e, len); *w = 1 + len; return 1;
}

static int dec_element(const uint8_t *b, size_t size, size_t *i, char *e, size_t cap)
{
    uint8_t t; size_t j;
    if (*i >= size) return 0;
    t = b[*i];
    if (t < 0x80) {
        int n = t >> 3 & 7, N = t & 7;
        if (cap < 12 || n == 0 || N > n) return 0;                  /* strict: no encoder makes these */
        snprintf(e, cap, "WIDE%d", n);
        if (N) snprintf(e + strlen(e), cap - strlen(e), "-%d", N);
        if (t & 0x40) strcat(e, "*");
        *i += 1; return 1;
    }
    if (t < 0xC0) {
        uint32_t v; char s[7]; int k, ssid; size_t l;
        j = *i + 1;
        if (j + 4 > size) return 0;
        v = (uint32_t)b[j] << 24 | (uint32_t)b[j + 1] << 16 | (uint32_t)b[j + 2] << 8 | b[j + 3]; j += 4;
        if (v >= 4096000000u) return 0;                             /* strict: 40^6 and above is no callsign */
        for (k = 5; k >= 0; k--) { if (v % 40 > 36) return 0; s[k] = B40[v % 40]; v /= 40; }   /* strict: digits 37 to 39 are no character */
        s[6] = 0;
        l = strlen(s); while (l && s[l - 1] == ' ') s[--l] = 0;
        if (!l || strchr(s, ' ')) return 0;                         /* strict: empty, or a space inside */
        ssid = t & 0x1F;
        if (ssid == 31) { if (j >= size) return 0; ssid = b[j++]; if (ssid < 31) return 0; }             /* strict: the extra byte is for SSIDs above 30 */
        if (cap < 16) return 0;
        strcpy(e, s);
        if (ssid) snprintf(e + strlen(e), cap - strlen(e), "-%d", ssid);
        if (t & 0x20) strcat(e, "*");
        *i = j; return 1;
    }
    { size_t n = t & 0x1F, q;
      if (n == 0 || *i + 1 + n > size || n + 1 > cap || memchr(b + *i + 1, 0, n)) return 0;
      for (q = 0; q < n; q++) if (b[*i + 1 + q] == ':' || b[*i + 1 + q] == ',' || b[*i + 1 + q] == '>') return 0;     /* strict: it would split the TNC2 packet in the wrong place */
      memcpy(e, b + *i + 1, n); e[n] = 0; *i += 1 + n; return 1; }
}

/* ---- the position block (kind 3) ---- */
typedef struct { int present; size_t offset; char text[16]; } alt_t;
static const char DTIS[] = "!=/@";
static void pad(char *out, unsigned v, int w) { char f[8]; snprintf(f, sizeof f, "%%0%du", w); snprintf(out, 16, f, v); }

static int b91dec(const char *s, int n, uint32_t *v)
{
    int k; uint32_t r = 0;
    for (k = 0; k < n; k++) { if (s[k] < 33 || s[k] > 123) return 0; r = r * 91 + (uint32_t)(s[k] - 33); }
    *v = r; return 1;
}
static void b91enc(uint32_t v, char *out) { int k; for (k = 3; k >= 0; k--) { out[k] = (char)(33 + v % 91); v /= 91; } out[4] = 0; }
static int is_digits(const char *s, int n) { int k; for (k = 0; k < n; k++) if (s[k] < '0' || s[k] > '9') return 0; return 1; }
static unsigned num(const char *s, int n) { unsigned v = 0; int k; for (k = 0; k < n; k++) v = v * 10 + (unsigned)(s[k] - '0'); return v; }

/* Parse the information field into a position block and the comment that remains; returns the block length (> 0) or 0. */
static size_t pos_encode(const char *info, size_t len, uint8_t *blk, char *comment, size_t *clen)
{
    size_t i = 1, w = 1; uint8_t flags = 0; const char *p; const char *dti;
    uint8_t tmp[32]; size_t tw = 0;
    if (len < 2) return 0;                                             /* before info[0] is read: the caller's buffer need not end with a NUL */
    dti = strchr(DTIS, info[0]);
    if (!dti || info[0] == 0) return 0;
    flags = (uint8_t)(dti - DTIS);
    if (info[0] == '/' || info[0] == '@') {
        uint32_t v; const char *f;
        if (len < i + 7 || !is_digits(info + i, 6) || !(f = strchr("zh/", info[i + 6])) || info[i + 6] == 0) return 0;
        v = num(info + i, 6) * 4 + (uint32_t)(f - "zh/");
        flags |= 0x04; blk[w++] = (uint8_t)(v >> 16); blk[w++] = (uint8_t)(v >> 8); blk[w++] = (uint8_t)v; i += 7;
    }
    p = info + i;
    if (len - i >= 1 && is_digits(p, 1)) {                                              /* uncompressed: ddmm.hhN T dddmm.hhE C */
        unsigned lat, lon; uint64_t v;
        if (len - i < 19 || !is_digits(p, 4) || p[4] != '.' || !is_digits(p + 5, 2) || (p[7] != 'N' && p[7] != 'S')
            || !is_digits(p + 9, 5) || p[14] != '.' || !is_digits(p + 15, 2) || (p[17] != 'E' && p[17] != 'W')) return 0;
        lat = num(p, 2) * 6000 + num(p + 2, 2) * 100 + num(p + 5, 2);
        lon = num(p + 9, 3) * 6000 + num(p + 12, 2) * 100 + num(p + 15, 2);
        v = ((uint64_t)(lat << 1 | (p[7] == 'S')) << 22) | (uint64_t)(lon << 1 | (p[17] == 'W'));
        { int k; for (k = 5; k >= 0; k--) tmp[tw++] = (uint8_t)(v >> (8 * k)); }
        tmp[tw++] = (uint8_t)p[8]; tmp[tw++] = (uint8_t)p[18];
        i += 19;
        if (len - i >= 7 && is_digits(info + i, 3) && info[i + 3] == '/' && is_digits(info + i + 4, 3)) {
            uint32_t cs = num(info + i, 3) * 1024 + num(info + i + 4, 3);
            flags |= 0x10; tmp[tw++] = (uint8_t)(cs >> 16); tmp[tw++] = (uint8_t)(cs >> 8); tmp[tw++] = (uint8_t)cs; i += 7;
        }
    } else {                                                                              /* compressed: T yyyy xxxx C csT */
        uint32_t la, lo; uint64_t v;
        if (len - i < 13 || !strchr("/\\ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij", p[0]) || !b91dec(p + 1, 4, &la) || !b91dec(p + 5, 4, &lo) || la >= (1u << 27) || lo >= (1u << 27)) return 0;
        v = ((uint64_t)la << 27) | lo;
        { int k; for (k = 6; k >= 0; k--) tmp[tw++] = (uint8_t)(v >> (8 * k)); }
        tmp[tw++] = (uint8_t)p[0]; tmp[tw++] = (uint8_t)p[9];
        flags |= 0x08;
        if (p[10] == ' ' && p[11] == ' ') { flags |= 0x40; tmp[tw++] = (uint8_t)p[12]; }
        else { tmp[tw++] = (uint8_t)p[10]; tmp[tw++] = (uint8_t)p[11]; tmp[tw++] = (uint8_t)p[12]; }
        i += 13;
    }
    memcpy(blk + w, tmp, tw); w += tw;
    /* altitude: "/A=dddddd" or "/A=-ddddd" anywhere in the comment moves into the block */
    { size_t cl = len - i, k; const char *c = info + i; long alt = 0; size_t aoff = (size_t)-1, alen = 0;
      for (k = 0; k + 9 <= cl && k <= 255; k++) {
          if (!strncmp(c + k, "/A=", 3)) {
              if (is_digits(c + k + 3, 6)) { aoff = k; alen = 9; alt = (long)num(c + k + 3, 6); break; }
              if (c[k + 3] == '-' && k + 9 <= cl && is_digits(c + k + 4, 5)) { aoff = k; alen = 9; alt = -(long)num(c + k + 4, 5); break; }
          }
      }
      if (aoff != (size_t)-1 && alt >= -999999 && alt <= 999999) {
          uint32_t a = (uint32_t)alt & 0xFFFFFF;
          flags |= 0x20; blk[w++] = (uint8_t)aoff; blk[w++] = (uint8_t)(a >> 16); blk[w++] = (uint8_t)(a >> 8); blk[w++] = (uint8_t)a;
          memcpy(comment, c, aoff); memcpy(comment + aoff, c + aoff + alen, cl - aoff - alen); *clen = cl - alen;
      } else { memcpy(comment, c, cl); *clen = cl; }
    }
    blk[0] = flags;
    return w;
}

static int pos_decode(const uint8_t *b, size_t size, size_t *i, char *prefix, size_t cap, alt_t *alt)
{
    uint8_t flags; char t[24]; uint64_t v; size_t j;
    if (*i >= size || cap < 80) return 0;
    flags = b[(*i)++]; alt->present = 0;
    if ((flags & 0x80) || ((flags & 0x10) && (flags & 0x08)) || ((flags & 0x40) && !(flags & 0x08)) || ((flags & 0x04) && (flags & 3) < 2)) return 0;   /* the timestamp belongs to / and @ only */   /* strict: reserved bit, impossible flag combinations */
    prefix[0] = DTIS[flags & 3]; prefix[1] = 0;
    if (flags & 0x04) {
        uint32_t tv;
        if (*i + 3 > size) return 0;
        tv = (uint32_t)b[*i] << 16 | (uint32_t)b[*i + 1] << 8 | b[*i + 2]; *i += 3;
        if (tv >> 2 > 999999 || (tv & 3) == 3) return 0;
        pad(t, tv >> 2, 6); strcat(prefix, t); { char z[2] = { "zh/"[tv & 3], 0 }; strcat(prefix, z); }
    }
    if (flags & 0x08) {
        char cst[4]; uint32_t la, lo; char s[6];
        if (*i + 9 > size) return 0;
        v = 0; for (j = 0; j < 7; j++) v = v << 8 | b[*i + j];
        *i += 7;
        la = (uint32_t)(v >> 27); lo = (uint32_t)(v & ((1u << 27) - 1));
        if (la >= 68574961u || lo >= 68574961u) return 0;
        if (!b[*i] || !b[*i + 1]) return 0;                           /* strict: no NUL */
        { char tb[2] = { (char)b[*i], 0 }, cd[2] = { (char)b[*i + 1], 0 }; *i += 2;
          strcat(prefix, tb); b91enc(la, s); strcat(prefix, s); b91enc(lo, s); strcat(prefix, s); strcat(prefix, cd); }
        if (flags & 0x40) { if (*i >= size || !b[*i]) return 0; cst[0] = ' '; cst[1] = ' '; cst[2] = (char)b[(*i)++]; cst[3] = 0; }
        else { if (*i + 3 > size || !b[*i] || !b[*i + 1] || !b[*i + 2]) return 0; memcpy(cst, b + *i, 3); cst[3] = 0; *i += 3; }
        strcat(prefix, cst);
    } else {
        unsigned la, lo, lat, lon; char tb[2], cd[2];
        if (*i + 8 > size) return 0;
        v = 0; for (j = 0; j < 6; j++) v = v << 8 | b[*i + j];
        *i += 6;
        la = (unsigned)(v >> 22); lo = (unsigned)(v & ((1u << 22) - 1)); lat = la >> 1; lon = lo >> 1;
        if (lat > 540000 || lon > 1080000) return 0;
        if (!b[*i] || !b[*i + 1]) return 0;
        tb[0] = (char)b[*i]; tb[1] = 0; cd[0] = (char)b[*i + 1]; cd[1] = 0; *i += 2;
        pad(t, lat / 6000, 2); strcat(prefix, t); pad(t, lat / 100 % 60, 2); strcat(prefix, t); strcat(prefix, "."); pad(t, lat % 100, 2); strcat(prefix, t);
        strcat(prefix, (la & 1) ? "S" : "N"); strcat(prefix, tb);
        pad(t, lon / 6000, 3); strcat(prefix, t); pad(t, lon / 100 % 60, 2); strcat(prefix, t); strcat(prefix, "."); pad(t, lon % 100, 2); strcat(prefix, t);
        strcat(prefix, (lo & 1) ? "W" : "E"); strcat(prefix, cd);
        if (flags & 0x10) {
            uint32_t cs;
            if (*i + 3 > size) return 0;
            cs = (uint32_t)b[*i] << 16 | (uint32_t)b[*i + 1] << 8 | b[*i + 2]; *i += 3;
            if ((cs >> 10) > 999 || (cs & 1023) > 999) return 0;      /* strict: course and speed are 3 digits */
            pad(t, cs >> 10, 3); strcat(prefix, t); strcat(prefix, "/"); pad(t, cs & 1023, 3); strcat(prefix, t);
        }
    }
    if (flags & 0x20) {
        long a;
        if (*i + 4 > size) return 0;
        alt->present = 1; alt->offset = b[(*i)++];
        a = (long)((uint32_t)b[*i] << 16 | (uint32_t)b[*i + 1] << 8 | b[*i + 2]); *i += 3;
        if (a & 0x800000) a -= 1L << 24;
        if (a < -99999 || a > 999999) return 0;                       /* strict: six digits, or - and five */
        if (a >= 0) { strcpy(alt->text, "/A="); pad(t, (unsigned)a, 6); strcat(alt->text, t); }
        else { strcpy(alt->text, "/A=-"); pad(t, (unsigned)(-a), 5); strcat(alt->text, t); }
    }
    return 1;
}

/* ---- TNC2 parsing ---- */
typedef struct { char src[40]; char dest[40]; char path[15][40]; int np; const char *info; size_t ilen; } tnc_t;

static int tnc_parse(const char *s, size_t len, tnc_t *t)
{
    const char *gt = memchr(s, '>', len), *colon, *p, *end;
    if (!gt || gt == s || gt - s >= 40 || memchr(s, ':', (size_t)(gt - s))) return 0;      /* a ':' in the source would split the packet in the wrong place */
    colon = memchr(gt, ':', len - (size_t)(gt - s));
    if (!colon) return 0;
    memcpy(t->src, s, (size_t)(gt - s)); t->src[gt - s] = 0;
    p = gt + 1; end = colon; t->np = 0;
    {   const char *c = memchr(p, ',', (size_t)(end - p)); const char *dend = c ? c : end;
        if (dend == p || dend - p >= 40) return 0;
        memcpy(t->dest, p, (size_t)(dend - p)); t->dest[dend - p] = 0;
        p = c ? c + 1 : end;
        while (c && p <= end) {
            const char *n = memchr(p, ',', (size_t)(end - p)); const char *eend = n ? n : end;
            if (eend == p || eend - p >= 40 || t->np >= 15) return 0;
            memcpy(t->path[t->np], p, (size_t)(eend - p)); t->path[t->np][eend - p] = 0; t->np++;
            if (!n) break;
            p = n + 1;
        }
    }
    t->info = colon + 1; t->ilen = len - (size_t)(colon + 1 - s);
    return 1;
}

#define CAPLEN 4                            /* capability bytes after V when V bit 7 is set */
/* The CRC of the proposal 3.0.0-dev.1 is taken over the bytes as sent, so a relay that does not hold the model can check it: the header kind bits (header & 7), V, the capability
 * bytes, the source and destination bytes (everything between the header and the path), the position block, L (LACF content only), and the payload or the plain text. The path and
 * the RXT block are not covered (relays change them), and the CRC field itself is left out. */
static uint16_t crc_wire(uint8_t hdr, uint8_t V, const uint8_t *capb, const uint8_t *srcdest, size_t sdn, const uint8_t *pos, size_t pn, int has_L, uint8_t L, const uint8_t *body, size_t bn)
{
    uint8_t buf[700]; size_t w = 0;
    if (2 + CAPLEN + sdn + pn + 1 + bn > sizeof buf) return 0;
    buf[w++] = hdr & 0x07; buf[w++] = V;
    if (capb) { memcpy(buf + w, capb, CAPLEN); w += CAPLEN; }
    memcpy(buf + w, srcdest, sdn); w += sdn;
    if (pn) { memcpy(buf + w, pos, pn); w += pn; }
    if (has_L) buf[w++] = L;
    memcpy(buf + w, body, bn); w += bn;
    return hb3_crc16(buf, w);
}

static size_t rxt_bytes(const hb3_rxt_t *r, uint8_t *out)
{
    size_t w = 0; int k, j; uint8_t mask = 0x80;
    for (k = 0; k < r->n; k++) { for (j = 0; j < 4; j++) out[w++] = r->t[k][j]; mask |= (uint8_t)(1 << r->idx[k]); }
    out[w++] = mask; return w;
}

int hb3_path_ok(const char *tnc2, size_t len)
{
    tnc_t t; int k;
    if (len > 600 || !tnc_parse(tnc2, len, &t)) return 1;                   /* other problems are for hb3_encode to report */
    for (k = 0; k < t.np; k++) if (!wide_compliant(t.path[k])) return 0;
    return 1;
}

size_t hb3_encode_cap(const char *tnc2, size_t len, const hb3_rxt_t *rxt, const uint8_t *capb, uint8_t *out, size_t cap, hb3_info_t *info)
{
    tnc_t t; uint8_t front[300], posblk[32], content[300], pay[300]; char comment[300]; char back[600]; hb3_info_t bi;
    size_t w = 0, fw, fwsd, pw = 0, cw, n, clen = 0, L, k, wr, crcpos, bn; int kind = 2, cc = 1; const uint8_t *textp, *bodyp; uint8_t V = VBYTE(capb != NULL), hdr;
    if (len > 600 || !tnc_parse(tnc2, len, &t) || t.ilen > 255) return 0;     /* the limits are on the frame (255) and the information field (255), not on the plain text */
    { int k0; for (k0 = 0; k0 < t.np; k0++) if (!wide_compliant(t.path[k0])) return 0; }   /* a non-compliant WIDE path is dropped, not sent in any form */
    /* kind: position block if the information field has one and round-trips */
    pw = pos_encode(t.info, t.ilen, posblk, comment, &clen);
    if (pw) {                                                           /* the block must give back the information field exactly, or kind 2 carries it */
        char pfx[96], rb[600]; alt_t a; size_t j = 0, off;
        if (!pos_decode(posblk, pw, &j, pfx, sizeof pfx, &a) || j != pw) pw = 0;
        else {
            off = a.present ? a.offset : 0;
            if (off > clen) pw = 0;
            else { size_t m = strlen(pfx); memcpy(rb, pfx, m); memcpy(rb + m, comment, off); m += off;
                   if (a.present) { size_t al = strlen(a.text); memcpy(rb + m, a.text, al); m += al; }
                   memcpy(rb + m, comment + off, clen - off); m += clen - off;
                   if (m != t.ilen || memcmp(rb, t.info, m)) pw = 0; }
        }
    }
    if (pw) kind = 3;
    if (kind == 3) { textp = (const uint8_t *)comment; L = clen; } else { textp = (const uint8_t *)t.info; L = t.ilen; }
    /* content: PPM when 4 + n < 3 + L (plain is V, a CRC-16 and the text: 3 + L), else plain. The CRC is filled in once the front is built (it covers the source and destination bytes). */
    cw = 0; crcpos = 0; bodyp = NULL; bn = 0;
    if (L) {
        n = (kind == 3) ? hbc_payload_encode(textp, L, pay, sizeof pay) : hbi_payload_encode(textp, L, pay, sizeof pay);
        if (n && 4 + n < 3 + L) {
            cc = 0;
            content[cw++] = V; if (capb) { memcpy(content + cw, capb, CAPLEN); cw += CAPLEN; } content[cw++] = (uint8_t)L;
            crcpos = cw; content[cw++] = 0; content[cw++] = 0;
            memcpy(content + cw, pay, n); cw += n; bodyp = pay; bn = n;
        }
    }
    if (cc == 1) {                                                /* plain content: kind 0 or 1 (kind - 2), no LACF flag; V, CRC-16 (little endian), text */
        content[cw++] = V; if (capb) { memcpy(content + cw, capb, CAPLEN); cw += CAPLEN; }
        crcpos = cw; content[cw++] = 0; content[cw++] = 0;
        memcpy(content + cw, textp, L); cw += L; bodyp = textp; bn = L; }
    /* front */
    hdr = (uint8_t)((t.np << 4) | ((rxt && rxt->n) ? 0x08 : 0) | (cc == 1 ? kind - 2 : kind));
    front[0] = 0x3C; front[1] = 0xFF; front[2] = 0x03; front[3] = hdr; fw = 4;
    if (!enc_element(t.src, front + fw, sizeof front - fw, &wr)) return 0;
    fw += wr;
    { int i, idx = -1; for (i = 0; i < nlist; i++) if (!strcmp(list[i], t.dest)) { idx = i; break; }
      if (idx >= 0) front[fw++] = (uint8_t)idx;
      else { front[fw++] = 127; if (!enc_element(t.dest, front + fw, sizeof front - fw, &wr)) return 0; fw += wr; } }
    fwsd = fw;
    { uint16_t crc = crc_wire(hdr, V, capb, front + 4, fwsd - 4, kind == 3 ? posblk : NULL, kind == 3 ? pw : 0, cc == 0, (uint8_t)L, bodyp, bn);
      content[crcpos] = (uint8_t)(crc & 0xFF); content[crcpos + 1] = (uint8_t)(crc >> 8); }
    for (k = 0; k < (size_t)t.np; k++) { if (!enc_element(t.path[k], front + fw, sizeof front - fw, &wr)) return 0; fw += wr; }
    w = 0;
    if (fw + (kind == 3 ? pw : 0) + cw + (rxt ? (size_t)rxt->n * 4 + 1 : 0) > cap) return 0;
    memcpy(out, front, fw); w = fw;
    if (kind == 3) { memcpy(out + w, posblk, pw); w += pw; }
    memcpy(out + w, content, cw); w += cw;
    if (rxt && rxt->n) w += rxt_bytes(rxt, out + w);
    if (w > 255) return 0;
    /* never emit a frame that does not round-trip */
    if (hb3_decode(out, w, back, sizeof back, &bi) != (int)len || memcmp(back, tnc2, len)) return 0;
    if (info) *info = bi;
    return w;
}

size_t hb3_encode(const char *tnc2, size_t len, const hb3_rxt_t *rxt, uint8_t *out, size_t cap, hb3_info_t *info)
{
    return hb3_encode_cap(tnc2, len, rxt, NULL, out, cap, info);
}

/* structural = 1: the checks that need no model (the front, the position block, V, the RXT block); the content is not decoded and the destination code may be unassigned.
 * The returned text then holds only "src>dest,path:" (spec 11, "Relay without the model"). */
static int decode_impl(const uint8_t *fr, size_t n, char *tnc2, size_t cap, hb3_info_t *info, int structural)
{
    uint8_t hdr; int kind, npath, k, have_rxt = 0; size_t i = 4, cs, end = n, rxt_start, front_len, path_start;
    char src[40], dest[40], path[15][40], prefix[96], comment[300], text[300], infof[700]; alt_t alt; hb3_rxt_t rx; int cc, ver, hascap, hasL = 0; uint8_t V, capb[CAPLEN] = {0, 0, 0, 0}; size_t b, bn, L = 0; const uint8_t *bodyp; uint16_t want;
    memset(&rx, 0, sizeof rx); alt.present = 0; prefix[0] = 0;
    if (n < 6 || fr[0] != 0x3C || fr[1] != 0xFF || fr[2] != 0x03) return HB3_ERR_PREFIX;
    if (n > 255) return HB3_ERR_SIZE;                                        /* a LoRa frame is at most 255 bytes; also keeps crc_wire inside its buffer (it returns 0 beyond it) */
    hdr = fr[3]; kind = hdr & 3; npath = hdr >> 4;
    if (hdr & 0x04) return HB3_ERR_RESERVED;                                 /* kinds 0 and 1 are plain content, 2 and 3 LACF content; kinds 1 and 3 have a position block */
    if (!dec_element(fr, n, &i, src, sizeof src)) return HB3_ERR_FRONT;
    if (i >= n) return HB3_ERR_FRONT;
    { uint8_t code = fr[i] & 0x7F;                                  /* bit 7 (c) is reserved and ignored */
      if (code == 126) return HB3_ERR_CODE_RESERVED;
      if (code == 127) { i++; if (!dec_element(fr, n, &i, dest, sizeof dest)) return HB3_ERR_FRONT; }
      else { if (code >= nlist) { if (!structural) return HB3_ERR_FRONT; strcpy(dest, "?"); } else strcpy(dest, list[code]); i++; } }
    path_start = i;
    for (k = 0; k < npath; k++) if (!dec_element(fr, n, &i, path[k], sizeof path[k]) || !wide_compliant(path[k])) return HB3_ERR_FRONT;
    front_len = i;
    if (kind & 1) { if (!pos_decode(fr, n, &i, prefix, sizeof prefix, &alt)) return HB3_ERR_POSITION; }
    cs = i;
    if (cs >= n) return HB3_ERR_CONTENT;
    /* RXT block at the end */
    if (hdr & 0x08) {
        uint8_t mask = fr[n - 1]; int kk = 0, j;
        if (!(mask & 0x80)) return HB3_ERR_RXT;
        for (j = 0; j < 7; j++) if (mask >> j & 1) { if (j >= npath) return HB3_ERR_RXT; kk++; }
        if (kk < 1 || n < cs + 1 + ((fr[cs] & 0x80) ? CAPLEN : 0) + (size_t)(4 * kk + 1)) return HB3_ERR_RXT;
        rxt_start = n - (size_t)(4 * kk + 1);
        { size_t p = rxt_start; int t2 = 0;
          for (j = 0; j < 7; j++) {
              int q;
              if (!(mask >> j & 1)) continue;
              rx.idx[t2] = (uint8_t)j;
              for (q = 0; q < 4; q++) { rx.t[t2][q] = fr[p++]; if (rx.t[t2][q] >= 0x80) return HB3_ERR_RANGE; }   /* 90 to 127 are unknown values (93 = not measured): the frame is kept */
              t2++;
          }
          rx.n = kk; }
        end = rxt_start; have_rxt = 1;
    }
    /* content */
    V = fr[cs]; cc = kind < 2 ? 3 : 0; ver = V & 31; hascap = V >> 7;      /* cc: 3 = plain (kinds 0 and 1), 0 = LACF (kinds 2 and 3) */
    b = cs + 1 + (hascap ? CAPLEN : 0);                                    /* the body starts after V and the capability bytes */
    if (hascap) { if (b > end) return HB3_ERR_CONTENT; memcpy(capb, fr + cs + 1, CAPLEN); }
    if (cc == 3) { if (end < b + 2) return HB3_ERR_CONTENT; want = (uint16_t)(fr[b] | fr[b + 1] << 8); bodyp = fr + b + 2; bn = end - b - 2; }
    else { if (end < b + 4) return HB3_ERR_CONTENT; L = fr[b]; if (L < 1) return HB3_ERR_CONTENT; want = (uint16_t)(fr[b + 1] | fr[b + 2] << 8); bodyp = fr + b + 3; bn = end - b - 3; hasL = 1; }
    /* the CRC is over the bytes as sent: it needs no model, so a relay without the model checks it too */
    if (crc_wire(hdr, V, hascap ? capb : NULL, fr + 4, path_start - 4, fr + front_len, cs - front_len, hasL, (uint8_t)L, bodyp, bn) != want) return HB3_ERR_CRC;
    if (cc == 3) {                                                         /* plain content uses no model: the model bits are ignored, so a relay without the model checks it like any receiver */
        size_t tl = bn;
        if (tl > 255) return HB3_ERR_CONTENT;
        if (memchr(bodyp, 0, tl)) return HB3_ERR_CONTENT;                 /* strict: no NUL in a TNC2 packet */
        if (kind & 1) { memcpy(comment, bodyp, tl); comment[tl] = 0; }
        else { memcpy(text, bodyp, tl); text[tl] = 0; }
        if (!(kind & 1)) { memcpy(infof, text, tl); infof[tl] = 0; }
        else {
            size_t off = alt.present ? alt.offset : 0;
            if (alt.present && off > tl) return HB3_ERR_POSITION;
            strcpy(infof, prefix);
            memcpy(infof + strlen(prefix), comment, off); infof[strlen(prefix) + off] = 0;
            if (alt.present) strcat(infof, alt.text);
            { size_t cur = strlen(infof); memcpy(infof + cur, comment + off, tl - off); infof[cur + tl - off] = 0; }
        }
        if (structural) infof[0] = 0;                                      /* the front only: the information field is not given */
        if (info) { info->cc = 1; info->payload_len = 0; }
    } else if (structural) {                                               /* LACF content without the model: the two rules that need only L and the payload length */
        if (4 + bn >= 3 + L) return HB3_ERR_SIZE;
        if (kind == 3 && alt.present && alt.offset > L) return HB3_ERR_POSITION;
        infof[0] = 0;
    } else {
        size_t pn = bn; int ok;
        if (ver != MODEL_VERSION) return HB3_ERR_VERSION;                  /* LACF content needs the model */
        ok = (kind == 3) ? hbc_payload_check(bodyp, pn, L, (uint8_t *)comment) : hbi_payload_check(bodyp, pn, L, (uint8_t *)text);
        if (!ok) return HB3_ERR_PAYLOAD;
        if (memchr(kind == 3 ? comment : text, 0, L)) return HB3_ERR_CONTENT;     /* strict: no NUL in a TNC2 packet */
        if (kind == 3) comment[L] = 0; else text[L] = 0;
        if (4 + pn >= 3 + L) return HB3_ERR_SIZE;
        if (kind == 2) { memcpy(infof, text, L); infof[L] = 0; }
        else {
            size_t off = alt.present ? alt.offset : 0;
            if (alt.present && off > L) return HB3_ERR_POSITION;
            strcpy(infof, prefix);
            memcpy(infof + strlen(prefix), comment, off); infof[strlen(prefix) + off] = 0;
            if (alt.present) strcat(infof, alt.text);
            { size_t cur = strlen(infof); memcpy(infof + cur, comment + off, L - off); infof[cur + L - off] = 0; }
        }
        if (info) { info->cc = 0; info->payload_len = pn; }
    }
    { size_t w; int q;
      w = (size_t)snprintf(tnc2, cap, "%s>%s", src, dest);
      for (q = 0; q < npath && w < cap; q++) w += (size_t)snprintf(tnc2 + w, cap - w, ",%s", path[q]);   /* cap - w must not wrap: stop as soon as the text no longer fits */
      if (w < cap) w += (size_t)snprintf(tnc2 + w, cap - w, ":%s", infof);
      if (w >= cap) return HB3_ERR_CONTENT;
      if (info) { info->kind = kind; info->ver = ver; info->npath = npath; info->frame_len = n; info->content_len = end - cs; info->front_len = front_len; info->path_start = path_start; info->content_start = cs; info->has_rxt = have_rxt; info->rxt = rx; info->has_cap = hascap; memcpy(info->cap, capb, CAPLEN); }
      return (int)w; }
}

int hb3_decode(const uint8_t *fr, size_t n, char *tnc2, size_t cap, hb3_info_t *info)
{
    return decode_impl(fr, n, tnc2, cap, info, 0);
}

/* The relay, once the new path is known. fr is the received frame (accepted by hb3_decode: in is its info, t its text parsed); t2 is the same packet with the
 * relay's path: only the path may differ. Copies the source and the destination exactly as received, the position block and the content byte for byte, keeps the
 * tuples of the earlier hops (each moves with its path element), and appends the relay's tuple at the element ownCall* of the new path. */
static size_t relay_core(const uint8_t *fr, size_t n, const hb3_info_t *in, const tnc_t *t, const tnc_t *t2, const char *relay_call, const uint8_t tuple[4], uint8_t *out, size_t cap)
{
    size_t i = 4, cs, end = n, w = 0, wr, pb = 0, from, fw = 4, base; int k, q, relay_idx = -1, own, np = t2->np; uint8_t hdr = fr[3]; char own_el[48]; hb3_rxt_t rx;
    uint8_t front[300];
    snprintf(own_el, sizeof own_el, "%s*", relay_call);
    for (k = 0; k < np; k++) {
        if (!wide_compliant(t2->path[k])) return 0;                         /* a non-compliant WIDE path is not sent in any form, whoever chose it */
        if (!strcmp(t2->path[k], own_el)) { if (relay_idx >= 0) return 0; relay_idx = k; }   /* our call used twice: the frame has already been through this relay, and a digipeater does not repeat what it repeated */
    }
    if (relay_idx < 0) return 0;
    own = relay_idx <= 6;
    if (own) for (q = 0; q < 4; q++) if (tuple[q] >= 0x80) return 0;        /* a tuple byte of 0x80 or more would look like the mask marker: the frame would be dropped by every receiver */                                                   /* mask bits 0 to 6 only: an eighth element or later relays without its own tuple */
    /* locate the parts to copy: re-walk the received front */
    { char e[40]; size_t j = 4;
      if (!dec_element(fr, n, &j, e, sizeof e)) return 0;
      j++; { uint8_t code = fr[j - 1] & 0x7F; if (code == 127) { if (!dec_element(fr, n, &j, e, sizeof e)) return 0; } }
      for (q = 0; q < in->npath; q++) if (!dec_element(fr, n, &j, e, sizeof e)) return 0;
      pb = j;                                                               /* the position block (kind 3) starts here, the content after it */
      if (fr[3] & 1) { char pfx[96]; alt_t a; if (!pos_decode(fr, n, &j, pfx, sizeof pfx, &a)) return 0; }
      i = j; }
    cs = i; if (in->has_rxt) { int kk = in->rxt.n; end = n - (size_t)(4 * kk + 1); }
    from = (hdr & 1) ? pb : cs;
    memset(&rx, 0, sizeof rx);
    if (in->has_rxt) for (q = 0; q < in->rxt.n; q++) {                      /* the earlier tuples, each with its path element */
        int oj = in->rxt.idx[q], m, pos, x;
        if (oj >= relay_idx || oj >= t->np) return 0;                       /* a tuple on the relay's own element or a later one: not relayed (spec 11, Relay, step 3) */
        if (oj < np && !strcmp(t2->path[oj], t->path[oj])) m = oj;
        else { for (m = 0; m < relay_idx; m++) if (!strcmp(t2->path[m], t->path[oj])) break; if (m == relay_idx) continue; }   /* the element is gone: its tuple goes with it */
        if (m > 6) continue;                                                /* the mask has bits 0 to 6 only: a tuple whose element moved to the eighth place or later cannot be carried */
        pos = rx.n; while (pos > 0 && rx.idx[pos - 1] > m) pos--;
        if (pos > 0 && rx.idx[pos - 1] == m) continue;
        for (x = rx.n; x > pos; x--) { rx.idx[x] = rx.idx[x - 1]; memcpy(rx.t[x], rx.t[x - 1], 4); }
        rx.idx[pos] = (uint8_t)m; memcpy(rx.t[pos], in->rxt.t[q], 4); rx.n++;
    }
    if (own && rx.n >= HB3_MAX_TUPLES) return 0;                              /* an eighth element or later adds no tuple, so it can relay a frame that already has seven */
    /* new front */
    front[0] = 0x3C; front[1] = 0xFF; front[2] = 0x03;
    if (in->path_start < 4 || in->path_start - 4 > sizeof front - 4) return 0;
    memcpy(front + fw, fr + 4, in->path_start - 4); fw += in->path_start - 4;   /* source and destination exactly as received (spec 11, Relay step 1) */
    for (k = 0; k < np; k++) { if (!enc_element(t2->path[k], front + fw, sizeof front - fw, &wr)) return 0; fw += wr; }
    /* size: a LoRa frame holds 255 bytes. A frame that would be longer with the relay's own tuple goes out without it (the earlier tuples stay); if it is too long even then, it is not relayed */
    base = fw + (end - from);
    if (own && (base + 4 * (size_t)(rx.n + 1) + 1 > 255 || base + 4 * (size_t)(rx.n + 1) + 1 > cap)) own = 0;
    if (base + (rx.n + own ? 4 * (size_t)(rx.n + own) + 1 : 0) > 255 || base + (rx.n + own ? 4 * (size_t)(rx.n + own) + 1 : 0) > cap) return 0;
    front[3] = (uint8_t)((np << 4) | ((rx.n + own) ? 0x08 : 0) | (hdr & 0x07));
    memcpy(out, front, fw); w = fw;
    /* position block and content copied byte for byte (everything between the old front and the old RXT block) */
    memcpy(out + w, fr + from, end - from); w += end - from;
    /* tuples: the old ones, then the relay's (its element index is the largest) */
    if (own) { for (q = 0; q < 4; q++) rx.t[rx.n][q] = tuple[q]; rx.idx[rx.n] = (uint8_t)relay_idx; rx.n++;
        if (rx.n > 1 && rx.idx[rx.n - 2] >= rx.idx[rx.n - 1]) return 0; }
    if (rx.n) w += rxt_bytes(&rx, out + w);
    (void)i; (void)cs;
    return w;
}

size_t hb3_relay_to(const uint8_t *fr, size_t n, const char *new_tnc2, size_t new_len, const char *relay_call, const uint8_t tuple[4], uint8_t *out, size_t cap)
{
    char tn[700]; hb3_info_t in; tnc_t t, t2; int len = hb3_decode(fr, n, tn, sizeof tn, &in);
    if (len <= 0 || !tnc_parse(tn, (size_t)len, &t) || !tnc_parse(new_tnc2, new_len, &t2)) return 0;
    if (strcmp(t.src, t2.src) || strcmp(t.dest, t2.dest) || t.ilen != t2.ilen || memcmp(t.info, t2.info, t.ilen)) return 0;    /* only the path may differ */
    return relay_core(fr, n, &in, &t, &t2, relay_call, tuple, out, cap);
}

static size_t relay_wide(const uint8_t *fr, size_t n, const char *relay_call, const uint8_t tuple[4], uint8_t *out, size_t cap, int structural)
{
    char tn[700]; hb3_info_t in; tnc_t t, t2; int len = decode_impl(fr, n, tn, sizeof tn, &in, structural);
    int k, relay_idx = -1; char tmp[40];
    if (len <= 0 || !tnc_parse(tn, (size_t)len, &t)) return 0;
    t2 = t;
    for (k = 0; k < t2.np; k++) {                                           /* the first element that is not yet used */
        size_t l = strlen(t2.path[k]);
        if (l && t2.path[k][l - 1] == '*') continue;
        if (!strcmp(t2.path[k], "WIDE1-1")) { snprintf(tmp, sizeof tmp, "%s*", relay_call); strcpy(t2.path[k], tmp); relay_idx = k; }
        else if (!strcmp(t2.path[k], "WIDE2-1")) { snprintf(tmp, sizeof tmp, "%s*", relay_call); strcpy(t2.path[k], tmp); relay_idx = k; }
        else if (!strcmp(t2.path[k], "WIDE2-2") && t2.np < 15) {
            int q; for (q = t2.np; q > k + 1; q--) strcpy(t2.path[q], t2.path[q - 1]);
            snprintf(tmp, sizeof tmp, "%s*", relay_call); strcpy(t2.path[k], tmp); strcpy(t2.path[k + 1], "WIDE2-1"); t2.np++; relay_idx = k;
        } else return 0;
        break;
    }
    if (relay_idx < 0) return 0;
    return relay_core(fr, n, &in, &t, &t2, relay_call, tuple, out, cap);
}

/* The front of a frame, whatever its model: the structural checks and the CRC (over the bytes as sent), no content decode. text gets "src>dest,path:" (the destination is "?" when its code
 * is not assigned in this list); a digipeater that does not hold the frame's model can apply its own path rule to it. Returns the text length, or a negative HB3_ERR_*. */
int hb3_decode_front(const uint8_t *fr, size_t n, char *text, size_t cap, hb3_info_t *info)
{
    return decode_impl(fr, n, text, cap, info, 1);
}

/* "Relay without the model" with the path chosen by the caller (the digipeater's own rule, applied to the text hb3_decode_front gave): new_tnc2 is that text with the new path, holding
 * relay_call + "*" as the element the relay used. Only the source and the destination must be those of the received frame; the content is not known. Same copy rules as hb3_relay_to. */
size_t hb3_relay_to_passthrough(const uint8_t *fr, size_t n, const char *new_tnc2, size_t new_len, const char *relay_call, const uint8_t tuple[4], uint8_t *out, size_t cap)
{
    char tn[700]; hb3_info_t in; tnc_t t, t2; int len = decode_impl(fr, n, tn, sizeof tn, &in, 1);
    if (len <= 0 || !tnc_parse(tn, (size_t)len, &t) || !tnc_parse(new_tnc2, new_len, &t2)) return 0;
    if (strcmp(t.src, t2.src) || strcmp(t.dest, t2.dest)) return 0;                 /* only the path may differ */
    return relay_core(fr, n, &in, &t, &t2, relay_call, tuple, out, cap);
}

size_t hb3_relay(const uint8_t *fr, size_t n, const char *relay_call, const uint8_t tuple[4], uint8_t *out, size_t cap)
{
    return relay_wide(fr, n, relay_call, tuple, out, cap, 0);
}

/* "Relay without the model" (spec 12, Relay step 8): every structural check and the CRC (which is over the bytes as sent), no content decode, so a frame whose model or destination code the relay does not hold is forwarded. */
size_t hb3_relay_passthrough(const uint8_t *fr, size_t n, const char *relay_call, const uint8_t tuple[4], uint8_t *out, size_t cap)
{
    return relay_wide(fr, n, relay_call, tuple, out, cap, 1);
}

/* The capability bytes (spec 12): role 3 | model decoded 5 (0 = plain only, else the newest model plus one) | SF 3 | BW 2 | CR 2 | power 6 | preset 5 | spare 6, 32 bits, most significant byte first. power = dBm (0 unknown, 1 to 63). */
static const int cap_bits[7] = { 3, 5, 3, 2, 2, 6, 5 };
int hb3_cap_pack(const int f[7], uint8_t out[4])
{
    uint32_t v = 0; int i;
    for (i = 0; i < 7; i++) { if (f[i] < 0 || f[i] >= (1 << cap_bits[i])) return 0; v = (v << cap_bits[i]) | (uint32_t)f[i]; }
    v <<= 6;                                                                /* six spare bits, sent as 0 */
    out[0] = (uint8_t)(v >> 24); out[1] = (uint8_t)(v >> 16); out[2] = (uint8_t)(v >> 8); out[3] = (uint8_t)v;
    return 1;
}
void hb3_cap_parse(const uint8_t b[4], int f[7])
{
    uint32_t v = (uint32_t)b[0] << 24 | (uint32_t)b[1] << 16 | (uint32_t)b[2] << 8 | b[3]; int i;
    v >>= 6;
    for (i = 6; i >= 0; i--) { f[i] = (int)(v & ((1u << cap_bits[i]) - 1)); v >>= cap_bits[i]; }
}
