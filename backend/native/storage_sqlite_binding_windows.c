/* Verify the actual loaded CPython SQLite imports, not a matching DLL name.
 * The caller owns approved module images and their complete code pins.
 * No file/process handles, DLL loading, registration or Python callbacks occur.
 * PE layout: https://learn.microsoft.com/windows/win32/debug/pe-format
 */
#include <windows.h>
#include <psapi.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include "sqlite3.h"

#ifndef API
#define API __declspec(dllexport)
#endif

typedef struct SqliteImage {
    unsigned char *base;
    DWORD size;
    IMAGE_NT_HEADERS64 header;
} SqliteImage;

static int sqlite_image_range(const SqliteImage *image, uint64_t offset, size_t count) {
    uint64_t finish;
    unsigned regions=0;
    if(offset>image->size || count>image->size-offset)return 0;
    finish=offset+count;
    while(offset<finish) {
        MEMORY_BASIC_INFORMATION region;
        uintptr_t address=(uintptr_t)image->base+(uintptr_t)offset;
        uint64_t end;
        if(++regions>128 || VirtualQuery((void*)address,&region,sizeof(region))!=sizeof(region)
            || region.State!=MEM_COMMIT || region.Type!=MEM_IMAGE || region.AllocationBase!=image->base
            || (region.Protect&(PAGE_GUARD|PAGE_NOACCESS))
            || !(region.Protect&(PAGE_READONLY|PAGE_READWRITE|PAGE_WRITECOPY|PAGE_EXECUTE_READ|
                                PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY)))return 0;
        if((uintptr_t)region.BaseAddress>(uintptr_t)image->base+offset)return 0;
        end=(uint64_t)((uintptr_t)region.BaseAddress-(uintptr_t)image->base)+region.RegionSize;
        if(end<=offset)return 0;
        offset=end<finish?end:finish;
    }
    return 1;
}

static int sqlite_image_read(const SqliteImage *image,uint64_t offset,void *out,size_t count) {
    if(!sqlite_image_range(image,offset,count))return 0;
    memcpy(out,image->base+(size_t)offset,count);
    return 1;
}

static int sqlite_image_string(const SqliteImage *image,uint64_t offset,char out[512]) {
    unsigned i;
    for(i=0;i<512;i++) {
        if(!sqlite_image_read(image,offset+i,out+i,1))return 0;
        if(!out[i])return i!=0;
        if((unsigned char)out[i]<32 || (unsigned char)out[i]>126)return 0;
    }
    return 0;
}

static int sqlite_image(HMODULE module,SqliteImage *image) {
    MODULEINFO info;
    IMAGE_DOS_HEADER dos;
    memset(image,0,sizeof(*image));
    if(!module || !K32GetModuleInformation(GetCurrentProcess(),module,&info,sizeof(info))
        || info.lpBaseOfDll!=(void*)module || info.SizeOfImage<4096 || info.SizeOfImage>256*1024*1024)return 0;
    image->base=info.lpBaseOfDll;image->size=info.SizeOfImage;
    if(!sqlite_image_read(image,0,&dos,sizeof(dos)) || dos.e_magic!=IMAGE_DOS_SIGNATURE || dos.e_lfanew<=0
        || !sqlite_image_read(image,(uint64_t)dos.e_lfanew,&image->header,sizeof(image->header)))return 0;
    return image->header.Signature==IMAGE_NT_SIGNATURE
        && image->header.FileHeader.Machine==IMAGE_FILE_MACHINE_AMD64
        && image->header.FileHeader.SizeOfOptionalHeader==sizeof(IMAGE_OPTIONAL_HEADER64)
        && image->header.OptionalHeader.Magic==IMAGE_NT_OPTIONAL_HDR64_MAGIC
        && image->header.OptionalHeader.SizeOfImage==image->size
        && image->header.OptionalHeader.NumberOfRvaAndSizes>IMAGE_DIRECTORY_ENTRY_IMPORT;
}

static int sqlite_image_function(const SqliteImage *image,uintptr_t address) {
    MEMORY_BASIC_INFORMATION region;
    if(address<(uintptr_t)image->base || address-(uintptr_t)image->base>=image->size)return 0;
    if(VirtualQuery((void*)address,&region,sizeof(region))!=sizeof(region)
        || region.State!=MEM_COMMIT || region.Type!=MEM_IMAGE || region.AllocationBase!=image->base
        || (region.Protect&(PAGE_GUARD|PAGE_NOACCESS)))return 0;
    return !!(region.Protect&(PAGE_EXECUTE_READ|PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY));
}

/* Return only native pointers for the independently selected SQLite module.
 * out: win32 VFS, registration, unregistration, version, verified import count.
 */
API DWORD gitgo_sqlite_bind(HMODULE extension,HMODULE selected,uint64_t *out,DWORD count) {
    SqliteImage python,sqlite;
    IMAGE_DATA_DIRECTORY directory;
    unsigned descriptor,imports=0,required=0,terminated=0,total=0;
    sqlite3_vfs *parent;
    sqlite3_vfs *(*find)(const char *);
    int (*version)(void);
    FARPROC reg,unreg;
    if(!out || count!=5)return ERROR_INVALID_PARAMETER;
    memset(out,0,5*sizeof(*out));
    if(sizeof(void*)!=8 || extension==selected || !sqlite_image(extension,&python)
        || !sqlite_image(selected,&sqlite))return ERROR_INVALID_DATA;
    directory=python.header.OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if(!directory.VirtualAddress || directory.Size<sizeof(IMAGE_IMPORT_DESCRIPTOR)
        || !sqlite_image_range(&python,directory.VirtualAddress,directory.Size))return ERROR_INVALID_DATA;
    for(descriptor=0;descriptor<128;descriptor++) {
        IMAGE_IMPORT_DESCRIPTOR item;
        uint64_t offset=(uint64_t)descriptor*sizeof(item);
        unsigned index,thunk_end=0;
        if(offset+sizeof(item)>directory.Size
            || !sqlite_image_read(&python,(uint64_t)directory.VirtualAddress+offset,&item,sizeof(item)))return ERROR_INVALID_DATA;
        if(!item.Name && !item.FirstThunk && !item.OriginalFirstThunk){terminated=1;break;}
        if(!item.Name || !item.FirstThunk || !item.OriginalFirstThunk)return ERROR_NOT_SUPPORTED;
        for(index=0;index<4096;index++) {
            IMAGE_THUNK_DATA64 named,resolved;
            uint64_t stride=(uint64_t)index*sizeof(named);
            char name[512];
            FARPROC expected;
            if(++total>4096 || !sqlite_image_read(&python,(uint64_t)item.OriginalFirstThunk+stride,&named,sizeof(named))
                || !sqlite_image_read(&python,(uint64_t)item.FirstThunk+stride,&resolved,sizeof(resolved)))return ERROR_INVALID_DATA;
            if(!named.u1.AddressOfData){if(resolved.u1.Function)return ERROR_INVALID_DATA;thunk_end=1;break;}
            if(IMAGE_SNAP_BY_ORDINAL64(named.u1.Ordinal))return ERROR_NOT_SUPPORTED;
            if(!sqlite_image_string(&python,named.u1.AddressOfData+offsetof(IMAGE_IMPORT_BY_NAME,Name),name))return ERROR_INVALID_DATA;
            if(strncmp(name,"sqlite3_",8))continue;
            expected=GetProcAddress(selected,name);
            if(!expected || (uintptr_t)expected!=(uintptr_t)resolved.u1.Function
                || !sqlite_image_function(&sqlite,(uintptr_t)expected))return ERROR_INVALID_DATA;
            imports++;
            if(!strcmp(name,"sqlite3_open_v2"))required|=1;
            if(!strcmp(name,"sqlite3_close") || !strcmp(name,"sqlite3_close_v2"))required|=2;
            if(!strcmp(name,"sqlite3_prepare_v2") || !strcmp(name,"sqlite3_prepare_v3"))required|=4;
            if(!strcmp(name,"sqlite3_step"))required|=8;
            if(!strcmp(name,"sqlite3_libversion"))required|=16;
        }
        if(!thunk_end)return ERROR_INVALID_DATA;
    }
    if(!terminated || required!=31 || !imports)return ERROR_NOT_SUPPORTED;
    find=(sqlite3_vfs *(*)(const char *))(uintptr_t)GetProcAddress(selected,"sqlite3_vfs_find");
    version=(int (*)(void))(uintptr_t)GetProcAddress(selected,"sqlite3_libversion_number");
    reg=GetProcAddress(selected,"sqlite3_vfs_register");unreg=GetProcAddress(selected,"sqlite3_vfs_unregister");
    if(!sqlite_image_function(&sqlite,(uintptr_t)find) || !sqlite_image_function(&sqlite,(uintptr_t)version)
        || !sqlite_image_function(&sqlite,(uintptr_t)reg) || !sqlite_image_function(&sqlite,(uintptr_t)unreg))return ERROR_INVALID_DATA;
    parent=find("win32");
    if(!parent || (uintptr_t)parent<(uintptr_t)sqlite.base
        || !sqlite_image_range(&sqlite,(uintptr_t)parent-(uintptr_t)sqlite.base,sizeof(*parent)))return ERROR_INVALID_DATA;
    if(parent->iVersion!=3 || parent->szOsFile<=0 || parent->szOsFile>4096
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xOpen)
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xDelete)
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xAccess)
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xFullPathname)
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xRandomness)
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xSleep)
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xCurrentTime)
        || !sqlite_image_function(&sqlite,(uintptr_t)parent->xCurrentTimeInt64)
        || (parent->xGetLastError && !sqlite_image_function(&sqlite,(uintptr_t)parent->xGetLastError))
        || parent->mxPathname<=0 || parent->mxPathname>=4096)return ERROR_INVALID_DATA;
    { char name[512];
      if((uintptr_t)parent->zName<(uintptr_t)sqlite.base
          || !sqlite_image_string(&sqlite,(uintptr_t)parent->zName-(uintptr_t)sqlite.base,name)
          || strcmp(name,"win32"))return ERROR_INVALID_DATA;
    }
    out[0]=(uint64_t)(uintptr_t)parent;out[1]=(uint64_t)(uintptr_t)reg;
    out[2]=(uint64_t)(uintptr_t)unreg;out[3]=(uint64_t)version();out[4]=imports;
    return 0;
}

API DWORD gitgo_sqlite_binding_abi(void){return 1;}
