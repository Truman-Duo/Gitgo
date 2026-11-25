using System;
using System.Runtime.InteropServices;

[ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface RecycleShellItem {
 void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
 void GetParent(out RecycleShellItem parent);
 void GetDisplayName(uint sigdnName, out IntPtr name);
 void GetAttributes(uint mask, out uint attributes);
 void Compare(RecycleShellItem other, uint hint, out int order);
}
[ComVisible(true), Guid("04B0F1A7-9490-44BC-96E1-4296A31252E2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface RecycleProgress {
 [PreserveSig] int StartOperations();
 [PreserveSig] int FinishOperations(int result);
 [PreserveSig] int PreRenameItem(uint flags, RecycleShellItem item, [MarshalAs(UnmanagedType.LPWStr)] string name);
 [PreserveSig] int PostRenameItem(uint flags, RecycleShellItem item, [MarshalAs(UnmanagedType.LPWStr)] string name, int result, RecycleShellItem created);
 [PreserveSig] int PreMoveItem(uint flags, RecycleShellItem item, RecycleShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string name);
 [PreserveSig] int PostMoveItem(uint flags, RecycleShellItem item, RecycleShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string name, int result, RecycleShellItem created);
 [PreserveSig] int PreCopyItem(uint flags, RecycleShellItem item, RecycleShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string name);
 [PreserveSig] int PostCopyItem(uint flags, RecycleShellItem item, RecycleShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string name, int result, RecycleShellItem created);
 [PreserveSig] int PreDeleteItem(uint flags, RecycleShellItem item);
 [PreserveSig] int PostDeleteItem(uint flags, RecycleShellItem item, int result, RecycleShellItem created);
 [PreserveSig] int PreNewItem(uint flags, RecycleShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string name);
 [PreserveSig] int PostNewItem(uint flags, RecycleShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string name, [MarshalAs(UnmanagedType.LPWStr)] string templateName, uint attributes, int result, RecycleShellItem created);
 [PreserveSig] int UpdateProgress(uint total, uint done);
 [PreserveSig] int ResetTimer();
 [PreserveSig] int PauseTimer();
 [PreserveSig] int ResumeTimer();
}
[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public class RecycleReceipt : RecycleProgress {
 public bool RefusedPermanentDelete {get;set;}
 public bool Completed {get;set;}
 public int Result {get;set;}
 public string RecyclePath {get;set;}
 public int StartOperations(){return 0;} public int FinishOperations(int r){return 0;}
 public int PreRenameItem(uint f,RecycleShellItem i,string n){return 0;}
 public int PostRenameItem(uint f,RecycleShellItem i,string n,int r,RecycleShellItem c){return 0;}
 public int PreMoveItem(uint f,RecycleShellItem i,RecycleShellItem d,string n){return 0;}
 public int PostMoveItem(uint f,RecycleShellItem i,RecycleShellItem d,string n,int r,RecycleShellItem c){return 0;}
 public int PreCopyItem(uint f,RecycleShellItem i,RecycleShellItem d,string n){return 0;}
 public int PostCopyItem(uint f,RecycleShellItem i,RecycleShellItem d,string n,int r,RecycleShellItem c){return 0;}
 public int PreDeleteItem(uint flags,RecycleShellItem item){
  if((flags&0x80)==0){RefusedPermanentDelete=true;return unchecked((int)0x80004004);}return 0;
 }
 public int PostDeleteItem(uint f,RecycleShellItem i,int r,RecycleShellItem c){
  Result=r; Completed=r>=0 && c!=null;
  if(c!=null){IntPtr p;try{c.GetDisplayName(0x80058000,out p);try{RecyclePath=Marshal.PtrToStringUni(p);}finally{Marshal.FreeCoTaskMem(p);}}catch{}}
  return 0;
 }
 public int PreNewItem(uint f,RecycleShellItem d,string n){return 0;}
 public int PostNewItem(uint f,RecycleShellItem d,string n,string t,uint a,int r,RecycleShellItem c){return 0;}
 public int UpdateProgress(uint t,uint d){return 0;} public int ResetTimer(){return 0;} public int PauseTimer(){return 0;} public int ResumeTimer(){return 0;}
}
[ComImport, Guid("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface RecycleFileOperation {
 void Advise(IntPtr sink,out uint cookie); void Unadvise(uint cookie);
 void SetOperationFlags(uint flags); void SetProgressMessage([MarshalAs(UnmanagedType.LPWStr)]string message);
 void SetProgressDialog(IntPtr dialog); void SetProperties(IntPtr properties); void SetOwnerWindow(uint window);
 void ApplyPropertiesToItem(RecycleShellItem item); void ApplyPropertiesToItems(IntPtr items);
 void RenameItem(RecycleShellItem item,[MarshalAs(UnmanagedType.LPWStr)]string name,IntPtr sink);
 void RenameItems(IntPtr items,[MarshalAs(UnmanagedType.LPWStr)]string name);
 void MoveItem(RecycleShellItem item,RecycleShellItem destination,[MarshalAs(UnmanagedType.LPWStr)]string name,IntPtr sink);
 void MoveItems(IntPtr items,RecycleShellItem destination);
 void CopyItem(RecycleShellItem item,RecycleShellItem destination,[MarshalAs(UnmanagedType.LPWStr)]string name,IntPtr sink);
 void CopyItems(IntPtr items,RecycleShellItem destination);
 void DeleteItem(RecycleShellItem item,RecycleProgress sink); void DeleteItems(IntPtr items);
 void NewItem(RecycleShellItem destination,uint attributes,[MarshalAs(UnmanagedType.LPWStr)]string name,[MarshalAs(UnmanagedType.LPWStr)]string templateName,IntPtr sink);
 void PerformOperations(); void GetAnyOperationsAborted([MarshalAs(UnmanagedType.Bool)]out bool aborted);
}
public static class SafeRecycle {
 [DllImport("shell32.dll",CharSet=CharSet.Unicode,PreserveSig=false)]
 static extern void SHCreateItemFromParsingName(string path,IntPtr context,ref Guid iid,out RecycleShellItem item);
 public static RecycleReceipt Send(string path){
  RecycleShellItem item=null; RecycleFileOperation operation=null;
  try{
   Guid iid=typeof(RecycleShellItem).GUID;SHCreateItemFromParsingName(path,IntPtr.Zero,ref iid,out item);
   operation=(RecycleFileOperation)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("3AD05575-8857-4850-9277-11B85BDB8E09")));
   operation.SetOperationFlags(0x20000000|0x00080000|0x00100000|0x00000400|0x00000010|0x00000004|0x00004000);
   var receipt=new RecycleReceipt();operation.DeleteItem(item,receipt);operation.PerformOperations();
   bool aborted;operation.GetAnyOperationsAborted(out aborted);
   if(aborted||receipt.RefusedPermanentDelete||!receipt.Completed)throw new InvalidOperationException("Recycle failed or permanent deletion was refused. HRESULT="+receipt.Result.ToString("X8"));
   return receipt;
  }finally{if(operation!=null)Marshal.FinalReleaseComObject(operation);if(item!=null)Marshal.FinalReleaseComObject(item);}
 }
}
