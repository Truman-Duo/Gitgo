#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
static BOOL (WINAPI *real_info)(HANDLE,FILE_INFO_BY_HANDLE_CLASS,LPVOID,DWORD)=GetFileInformationByHandleEx;
static int mode,queries;
static BOOL WINAPI injected_info(HANDLE h,FILE_INFO_BY_HANDLE_CLASS kind,LPVOID out,DWORD length){
    BOOL ok=real_info(h,kind,out,length);
    if(kind!=FileIdBothDirectoryInfo&&kind!=FileIdBothDirectoryRestartInfo)return ok;
    queries++;
    if(mode==1&&ok){SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    if(mode==2&&ok){((FILE_ID_BOTH_DIR_INFO*)out)->FileNameLength=UINT32_MAX;}
    if(mode==3&&ok){((FILE_ID_BOTH_DIR_INFO*)out)->NextEntryOffset=UINT32_MAX;}
    if(mode==4&&!ok&&GetLastError()==ERROR_NO_MORE_FILES){SetLastError(ERROR_ACCESS_DENIED);return FALSE;}
    return ok;
}
#define GetFileInformationByHandleEx injected_info
#include "../../backend/native/storage_tree_windows.c"
int wmain(int argc,wchar_t **argv){
    HANDLE token=0,parent;FILE_ID_INFO id;uint64_t identity[3],cookie,status[8];
    unsigned char buffer[1024];DWORD length,used,count;PSID sid;wchar_t output[33025];int before;
    assert(argc==3);mode=_wtoi(argv[2]);assert(mode>=1&&mode<=4);
    assert(OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token));
    assert(GetTokenInformation(token,TokenUser,buffer,sizeof(buffer),&length));sid=((TOKEN_USER*)buffer)->User.Sid;
    assert(CloseHandle(token));length=GetLengthSid(sid);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&real_info(parent,FileIdInfo,&id,sizeof(id)));
    identity[0]=id.VolumeSerialNumber;memcpy(identity+1,id.FileId.Identifier,16);
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_PIN));
    assert(!gitgo_tree_root(cookie,parent,identity,3));
    assert(gitgo_tree_inventory(cookie,0,output,33025,&used,&count)!=0);
    before=queries;assert(before>0);
    assert(gitgo_tree_inventory(cookie,0,output,33025,&used,&count)==ERROR_INVALID_PARAMETER&&queries==before);
    assert(!gitgo_tree_status(cookie,status,8)&&status[0]==1&&status[2]==1);
    assert(!gitgo_tree_retire(cookie)&&CloseHandle(parent));
    printf("native inventory fault %d passed; incomplete enumeration never accepted\n",mode);
    return 0;
}
