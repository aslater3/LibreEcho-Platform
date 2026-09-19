#define _GNU_SOURCE
#include "pcm_stream_server.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"pcm stream line %d: %s\n",__LINE__,#x); exit(1); } } while (0)
static struct le_pcm_server server;
static char root[] = "/tmp/le-pcm-XXXXXX";
static int connect_source(unsigned int role, int focus)
{
    struct sockaddr_un address = {0};
    unsigned char message[LE_PCM_HEADER];
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK, 0);
    CHECK(fd >= 0);
    address.sun_family = AF_UNIX;
    CHECK(snprintf(address.sun_path,sizeof(address.sun_path),"%s/streams.sock",root) > 0);
    CHECK(connect(fd,(struct sockaddr *)&address,sizeof(address)) == 0);
    le_pcm_header(message,LE_PCM_OPEN,0,role | (focus ? LE_PCM_FOCUS : 0));
    CHECK(send(fd,message,sizeof(message),0) == (ssize_t)sizeof(message));
    le_pcm_server_service(&server);
    return fd;
}
static void command(int fd, unsigned int kind)
{
    unsigned char message[LE_PCM_HEADER];
    le_pcm_header(message,kind,0,0);
    CHECK(send(fd,message,sizeof(message),0) == (ssize_t)sizeof(message));
}
static void data(int fd, unsigned int frames, int16_t sample)
{
    unsigned char message[LE_PCM_PACKET_BYTES];
    unsigned int i;
    CHECK(frames && frames <= LE_PCM_PACKET_FRAMES);
    le_pcm_header(message,LE_PCM_DATA,frames,0);
    for (i=0;i<frames*2U;++i) {
        message[LE_PCM_HEADER+i*2U]=(unsigned char)sample;
        message[LE_PCM_HEADER+i*2U+1]=(unsigned char)((uint16_t)sample>>8);
    }
    CHECK(send(fd,message,LE_PCM_HEADER+frames*4U,0) == (ssize_t)(LE_PCM_HEADER+frames*4U));
}
static unsigned int progress(int fd, uint64_t *accepted, uint64_t *played)
{
    unsigned char message[LE_PCM_HEADER];
    unsigned int state=0;
    ssize_t n;
    while ((n=recv(fd,message,sizeof(message),MSG_DONTWAIT)) > 0) {
        CHECK(n == LE_PCM_HEADER && le_pcm_get32(message+4) == LE_PCM_STATE);
        state=le_pcm_get32(message+8);
        *accepted=le_pcm_get64(message+16); *played=le_pcm_get64(message+24);
    }
    CHECK(n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK));
    return state;
}
static void finite(unsigned int frames)
{
    int fd=connect_source(1,1);
    unsigned int left=frames, period, index;
    uint64_t accepted=0,played=0,hardware=0,submitted=0;
    while (left) {
        unsigned int n=left > 1024 ? 1024 : left;
        data(fd,n,1234); left-=n;
    }
    command(fd,LE_PCM_FINISH); le_pcm_server_service(&server);
    CHECK(le_pcm_server_ready(&server,2048));
    for (period=0; submitted<frames; ++period) {
        unsigned int valid=frames-submitted > 2048 ? 2048 : (unsigned int)(frames-submitted);
        CHECK(period < 4);
        for(index=0;index<2048;++index)
            CHECK(le_pcm_server_mix(&server,index,2048,32768) == (index < valid ? 1234 : 0));
        le_pcm_server_submit(&server,hardware,2048);
        if (valid > 1) {
            le_pcm_server_progress(&server,hardware+valid-1);
            CHECK(progress(fd,&accepted,&played) != LE_PCM_DRAINED);
        }
        le_pcm_server_progress(&server,hardware+valid);
        submitted+=valid; hardware+=2048;
        if (submitted<frames) le_pcm_server_service(&server);
    }
    CHECK(progress(fd,&accepted,&played) == LE_PCM_DRAINED);
    CHECK(accepted==frames && played==frames && !le_pcm_server_focus(&server));
    close(fd); le_pcm_server_service(&server);
}
static void gaps_and_cancellation(void)
{
    int speech=connect_source(1,1),cue=connect_source(1,0),music=connect_source(0,0);
    uint64_t accepted=0,played=0;
    data(speech,1,200); le_pcm_server_service(&server);
    CHECK(!le_pcm_server_ready(&server,2048)); /* No premature padding between packets. */
    CHECK(le_pcm_server_focus(&server));
    data(cue,1,300); command(cue,LE_PCM_FINISH);
    data(music,1024,100); data(music,1024,100);
    le_pcm_server_service(&server);
    CHECK(le_pcm_server_mix(&server,0,2048,32768)==400); /* Speech not ready yet. */
    /* Cancellation bypasses DATA already in transit, scoped to this connection. */
    data(speech,1024,200); close(speech); le_pcm_server_service(&server);
    CHECK(le_pcm_server_mix(&server,0,2048,32768)==400);
    CHECK(le_pcm_server_mix(&server,1,2048,32768)==100);
    CHECK(!le_pcm_server_focus(&server));
    le_pcm_server_submit(&server,8192,2048);
    le_pcm_server_progress(&server,8193);
    CHECK(progress(cue,&accepted,&played)==LE_PCM_DRAINED && played==1);
    close(cue);
    /* A new generation cannot be cleared by its predecessor's disconnect. */
    speech=connect_source(1,1); data(speech,1,700); command(speech,LE_PCM_FINISH);
    le_pcm_server_service(&server);
    CHECK(le_pcm_server_mix(&server,0,2048,32768)==700);
    le_pcm_server_submit(&server,10240,2048);
    le_pcm_server_progress(&server,10241);
    CHECK(progress(speech,&accepted,&played)==LE_PCM_DRAINED && played==1);
    CHECK(progress(music,&accepted,&played)==LE_PCM_ACCEPTING && played==2048);
    close(speech);close(music);le_pcm_server_service(&server);
}
static void bounds_and_errors(void)
{
    int fd=connect_source(2,1), i;
    for(i=0;i<8;++i) data(fd,1024,10);
    le_pcm_server_service(&server);
    for(i=0;i<(int)LE_PCM_CLIENTS;++i) CHECK(server.source[i].queued<=LE_PCM_QUEUE_FRAMES);
    close(fd);le_pcm_server_service(&server);
    CHECK(!le_pcm_server_ready(&server,2048));
    fd=connect_source(1,1);data(fd,1,5);command(fd,LE_PCM_FINISH);le_pcm_server_service(&server);
    data(fd,1,9);le_pcm_server_service(&server); /* DATA after FINISH is forbidden. */
    CHECK(server.failures>0 && !le_pcm_server_ready(&server,2048));
    close(fd);
}
int main(void)
{
    CHECK(mkdtemp(root)); CHECK(le_pcm_server_open(&server,root)==0);
    finite(1);finite(2047);finite(2048);finite(2049);finite(4097);
    gaps_and_cancellation();bounds_and_errors();
    le_pcm_server_close(&server); CHECK(rmdir(root)==0);
    puts("pcm streams: exact tails, gaps, independent progress/cancellation and bounds PASS");
    return 0;
}
