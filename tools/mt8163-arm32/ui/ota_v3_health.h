/* Platform-owned v3 status markers. Bounded, nonblocking, no-follow reads. */
#ifndef LE_OTA_V3_HEALTH_H
#define LE_OTA_V3_HEALTH_H
#include <ctype.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#ifndef LE_OTA_STATUS_ROOT
#define LE_OTA_STATUS_ROOT "/data/libreecho/update"
#endif
static void le_ota_status_value(const char *name,const char *key,char *out,size_t capacity)
{
    char path[512],buffer[1025],*line,*end; struct stat st; int fd; ssize_t count;
    snprintf(path,sizeof(path),"%s/%s",LE_OTA_STATUS_ROOT,name);
    fd=open(path,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if(fd<0)return;
    if(fstat(fd,&st)||!S_ISREG(st.st_mode)||st.st_size>1024){close(fd);return;}
    count=read(fd,buffer,sizeof(buffer)-1); close(fd);
    if(count<0)return;
    buffer[count]=0;
    for(line=buffer;line&&*line;line=end){
        size_t i,n; end=strchr(line,'\n'); if(end)*end++=0;
        n=strlen(key); if(strncmp(line,key,n)||line[n]!='=')continue;
        line+=n+1; n=strlen(line); if(n>=capacity)return;
        for(i=0;i<n;i++)if(!isalnum((unsigned char)line[i])&&line[i]!='_'&&line[i]!='-'&&line[i]!=':')return;
        memcpy(out,line,n+1); return;
    }
}
static void le_ota_health_json(char *out,size_t size)
{
    char state[24]="unknown",feature[161]="",config[161]="";
    le_ota_status_value("features-status","features_state",state,sizeof(state));
    if(strcmp(state,"ready")&&strcmp(state,"degraded"))strcpy(state,"unknown");
    le_ota_status_value("features-status","features_error",feature,sizeof(feature));
    le_ota_status_value("config-status","config_error",config,sizeof(config));
    snprintf(out,size,",\"features_state\":\"%s\",\"features_error\":\"%s\",\"config_error\":\"%s\"",state,feature,config);
}
#endif
