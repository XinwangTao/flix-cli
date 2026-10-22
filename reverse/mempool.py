"""Read the live flix.exe process to resolve Dart AOT object-pool entries.

Method:
1. Enumerate committed memory regions of the target process.
2. Locate known Dart OneByteString objects by ASCII pattern + length check.
3. Search all readable memory for 8-byte pointers to those objects
   (these are ObjectPool entries of the deserialized snapshot).
4. Calibrate the pool object base and emit a disp->string candidate map.

Usage: python mempool.py <pid> <probe-strings-file> <out.json>
"""
import ctypes
import ctypes.wintypes as wt
import json
import sys

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_READABLE = {0x02, 0x04, 0x06, 0x20, 0x40}  # READONLY, READWRITE, EXECUTE_READ, EXECUTE_READWRITE etc.

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD)]


def open_proc(pid: int):
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    return h


def regions(h):
    addr = 0
    out = []
    mbi = MBI()
    while True:
        res = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not res:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in PAGE_READABLE and mbi.Type == 0x20000:
            out.append((mbi.BaseAddress, mbi.RegionSize))
        addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        if addr > 0x7FFFFFFEFFFF:
            break
    return out


def read(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


def find_all(data: bytes, needle: bytes, limit: int = 64):
    out = []
    i = data.find(needle)
    while i != -1 and len(out) < limit:
        out.append(i)
        i = data.find(needle, i + 1)
    return out


def main():
    pid = int(sys.argv[1])
    probes = [s.strip() for s in open(sys.argv[2], encoding="utf-8").read().splitlines() if s.strip()]
    h = open_proc(pid)
    regs = regions(h)
    total = sum(r[1] for r in regs)
    print(f"regions={len(regs)} total={total/1e6:.1f}MB", file=sys.stderr)

    # 1) locate probe string objects (ascii + len check, one-byte strings)
    obj_addr = {}  # string -> (heap_addr_of_object_start)
    for base, size in regs:
        if size > 64 * 1024 * 1024:
            continue
        blob = read(h, base, size)
        if not blob:
            continue
        for s in probes:
            if s in obj_addr:
                continue
            nb = s.encode()
            for i in find_all(blob, nb, limit=8):
                if i >= 16:
                    ln = int.from_bytes(blob[i - 8:i - 4], "little")
                    if ln == len(s) * 2:  # Smi-encoded length
                        obj_addr[s] = base + i - 16
    print(f"resolved {len(obj_addr)}/{len(probes)} probe strings", file=sys.stderr)

    # 2) search pointer candidates (qword == obj start)
    targets = {a: s for s, a in obj_addr.items()}
    entries = {}  # slot_addr -> string
    for base, size in regs:
        if size > 256 * 1024 * 1024:
            continue
        blob = read(h, base, size)
        if not blob:
            continue
        for i in range(0, len(blob) - 8, 8):
            v = int.from_bytes(blob[i:i + 8], "little")
            if v in targets:
                entries[base + i] = targets[v]
    print(f"pool-entry candidates: {len(entries)}", file=sys.stderr)
    json.dump({"strings": obj_addr, "entries": entries},
              open(sys.argv[3], "w"), indent=1)


if __name__ == "__main__":
    main()
