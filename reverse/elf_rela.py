"""Parse Dart AOT ELF (app.so): build .rela.dyn index + sections map.

Output: JSON summary {sections, rela_count, rela_sample} printed once;
full index kept in memory for companion tools (import elf_rela).
"""
import json
import struct
import sys

ELF_PATH = r"D:\flix-cli\reverse\ida\app.so"


def load(path: str = ELF_PATH):
    data = open(path, "rb").read()
    assert data[:4] == b"\x7fELF"
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", data, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", data, 0x3E)[0]
    sections = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name, typ, flags, addr, offset, size, link, info, align, entsize = struct.unpack_from(
            "<IIQQQQIIQQ", data, off)
        sections.append(dict(name_off=name, type=typ, addr=addr, offset=offset,
                             size=size, link=link, entsize=entsize))
    strtab = sections[e_shstrndx]
    def sname(noff):
        end = data.index(b"\x00", strtab["offset"] + noff)
        return data[strtab["offset"] + noff:end].decode()
    for s in sections:
        s["name"] = sname(s["name_off"])

    rela_by_offset = {}
    rela_by_addend = {}
    rela_sections = [s for s in sections if s["type"] in (4, 7)]  # RELA, RELA? (4=RELA)
    for s in rela_sections:
        n = s["size"] // 24
        base = s["offset"]
        for i in range(n):
            off, info, addend = struct.unpack_from("<QQq", data, base + i * 24)
            rtype = info & 0xFFFFFFFF
            if rtype == 8:  # R_X86_64_RELATIVE
                rela_by_offset[off] = addend
                rela_by_addend.setdefault(addend, []).append(off)
    return data, sections, rela_by_offset, rela_by_addend


if __name__ == "__main__":
    data, sections, by_off, by_addend = load()
    print(json.dumps({
        "sections": [{"name": s["name"], "addr": hex(s["addr"]), "size": hex(s["size"])}
                     for s in sections if s["size"] > 0 and s["addr"] > 0],
        "rela_relative_count": len(by_off),
        "sample_by_offset": {hex(k): hex(v) for k, v in list(by_off.items())[:5]},
    }, indent=1))
