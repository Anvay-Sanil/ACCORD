"""Trusted bootstrap installs OS limits before importing the PDF parser."""
import ctypes
import json
import os
import sys

WINDOWS_JOB = None


def limits():
    global WINDOWS_JOB
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU,(5,5))
        resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
        resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
        return
    if os.name != "nt":
        raise RuntimeError("No supported process resource limiter")
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit",ctypes.c_longlong),("PerJobUserTimeLimit",ctypes.c_longlong),
          ("LimitFlags",w.DWORD),("MinimumWorkingSetSize",ctypes.c_size_t),("MaximumWorkingSetSize",ctypes.c_size_t),
          ("ActiveProcessLimit",w.DWORD),("Affinity",ctypes.c_size_t),("PriorityClass",w.DWORD),("SchedulingClass",w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(x,ctypes.c_ulonglong) for x in ("ReadOperationCount","WriteOperationCount","OtherOperationCount",
                                                  "ReadTransferCount","WriteTransferCount","OtherTransferCount")]
    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation",Basic),("IoInfo",IO),("ProcessMemoryLimit",ctypes.c_size_t),
                    ("JobMemoryLimit",ctypes.c_size_t),("PeakProcessMemoryUsed",ctypes.c_size_t),("PeakJobMemoryUsed",ctypes.c_size_t)]
    k = ctypes.WinDLL("kernel32",use_last_error=True)
    k.CreateJobObjectW.restype = w.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p,w.LPCWSTR]
    k.GetCurrentProcess.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
    WINDOWS_JOB = k.CreateJobObjectW(None,None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x100 | 0x2  # process memory, process CPU time
    info.BasicLimitInformation.PerProcessUserTimeLimit = 5*10_000_000
    info.ProcessMemoryLimit = 512*1024*1024
    if not WINDOWS_JOB or not k.SetInformationJobObject(WINDOWS_JOB,9,ctypes.byref(info),ctypes.sizeof(info)) or not k.AssignProcessToJobObject(WINDOWS_JOB,k.GetCurrentProcess()):
        raise RuntimeError(f"Cannot establish Windows Job limits: {ctypes.get_last_error()}")


def main():
    limits()
    import importlib.metadata
    import pdfplumber
    spans = []
    with pdfplumber.open(sys.argv[1]) as pdf:
        if len(pdf.pages) > 10:
            raise ValueError("Page limit exceeded")
        for page_no,page in enumerate(pdf.pages,1):
            for line_no,line in enumerate(page.extract_text_lines(),1):
                text = line["text"]
                spans.append({"id":f"p{page_no}-l{line_no}","page":page_no,"text":text,
                    "bbox":[line["x0"],line["top"],line["x1"],line["bottom"]]})
                if len(spans)>2000 or sum(len(s["text"]) for s in spans)>100000:
                    raise ValueError("Text limit exceeded")
    if not spans:
        raise ValueError("No extractable text")
    result = json.dumps({"parser":"pdfplumber-"+importlib.metadata.version("pdfplumber")+"/lines-v1","spans":spans})
    if len(result)>1_000_000:
        raise ValueError("Output too large")
    sys.stdout.write(result)


if __name__ == "__main__":
    main()

