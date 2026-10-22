"""Full [r15+disp32] load scan from the on-disk app.so (all REX.B forms).
Output: code_loads_full.json {hex_addr: disp}
"""
import json, struct, sys

SO = r"D:\flix-cli\reverse\ida\app.so"
data = open(SO, 'rb').read()
assert data[:4] == b'\x7fELF'
# program headers
e_phoff = struct.unpack_from('<Q', data, 0x20)[0]
e_phentsize = struct.unpack_from('<H', data, 0x36)[0]
e_phnum = struct.unpack_from('<H', data, 0x38)[0]
segs = []
for i in range(e_phnum):
    off = e_phoff + i * e_phentsize
    p_type, p_flags = struct.unpack_from('<II', data, off)
    p_offset, p_vaddr, _pa, p_filesz, _memsz, _al = struct.unpack_from('<QQQQQQ', data, off + 8)
    if p_type == 1 and (p_flags & 1):  # PT_LOAD + X
        segs.append((p_vaddr, p_offset, p_filesz))
print('exec segments:', [(hex(v), hex(f)) for v, f, _ in segs], file=sys.stderr)

out = {}
MODRM = set()
for reg in range(8):
    MODRM.add(0x80 | (reg << 3) | 7)  # mod=10 rm=111
count = 0
for vaddr, off, sz in segs:
    blob = data[off:off + sz]
    n = len(blob)
    i = 0
    while i < n - 7:
        b = blob[i]
        if b in (0x41, 0x43, 0x45, 0x47, 0x49, 0x4B, 0x4D, 0x4F) and blob[i+1] == 0x8B \
           and blob[i+2] in MODRM:
            disp = int.from_bytes(blob[i+3:i+7], 'little', signed=False)
            out[hex(vaddr + i)] = disp
            i += 7
            count += 1
            continue
        # REX + 0F 10/11/28/29 (SSE) with modrm disp32: 44 0F 10 87? rare; skip for now
        i += 1
json.dump(out, open(r'D:\flix-cli\reverse\code_loads_full.json', 'w'))
print('scanned loads:', count, '-> code_loads_full.json', file=sys.stderr)
