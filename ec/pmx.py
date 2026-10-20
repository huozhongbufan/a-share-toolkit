"""PMX 2.0 结构解析（只读信息 + 供渲染用）。

格式依据：PMX 2.0 规范（官方文档公开格式）。
头 17 字节：'PMX '(4) + 版本float(4) + 设置字节数(1) + 设置(8)
  设置 = [编码, 附加UV数, 顶点索引尺寸, 贴图索引尺寸, 材质索引尺寸,
          骨骼索引尺寸, 变形索引尺寸, 刚体索引尺寸]
实测本模型（瑶瑶皮肤.pmx）：
  编码=0(UTF-16) 附加UV=0 索引尺寸 2/1/1/2/1/2
  模型名 JP/EN = 'YaoYao'

之前踩的坑（写在这里防止再犯）：
  · 头是 17 字节不是 20 —— 从 20 开始读设置，读到的是模型名字节
  · 编码=0 时所有字符串是 UTF-16LE，不是 UTF-8
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class Material:
    name_jp: str = ""
    name_en: str = ""
    diffuse: tuple = (1, 1, 1, 1)
    specular: tuple = (0, 0, 0)
    spec_strength: float = 0
    ambient: tuple = (0, 0, 0)
    flags: int = 0
    edge_color: tuple = (0, 0, 0, 1)
    edge_size: float = 1
    tex_idx: int = -1
    sphere_idx: int = -1
    sphere_mode: int = 0
    toon: int = 0          # 0=共享toon索引, 1=贴图索引
    toon_value: int = 0
    memo: str = ""
    face_count: int = 0


@dataclass
class Vertex:
    pos: tuple = (0, 0, 0)
    normal: tuple = (0, 0, 1)
    uv: tuple = (0, 0)
    weight_type: int = 0
    bones: tuple = ()       # (b1, b2, b3, b4) 索引
    weights: tuple = ()     # 对应权重
    sdef_c: tuple = (0, 0, 0)
    sdef_r0: tuple = (0, 0, 0)
    sdef_r1: tuple = (0, 0, 0)


@dataclass
class Bone:
    name_jp: str = ""
    name_en: str = ""
    pos: tuple = (0, 0, 0)
    parent: int = -1
    layer: int = 0
    flags: int = 0
    tail_pos: tuple = (0, 0, 0)   # 或 tail_index
    tail_index: int = -1
    inherit_index: int = -1
    inherit_ratio: float = 0


@dataclass
class Pmx:
    name_jp: str = ""
    name_en: str = ""
    vertices: list = field(default_factory=list)
    indices: list = field(default_factory=list)
    textures: list = field(default_factory=list)
    materials: list = field(default_factory=list)
    bones: list = field(default_factory=list)
    morph_names: list = field(default_factory=list)
    morph_counts: dict = field(default_factory=dict)


class Parser:
    def __init__(self, data: bytes):
        self.data = data
        self.off = 0
        assert data[:4] == b"PMX ", "不是 PMX 文件"
        self.version = struct.unpack_from("<f", data, 4)[0]
        self.settings_count = data[8]
        (self.encoding, self.n_uv, self.vi_sz, self.ti_sz,
         self.mi_sz, self.bi_sz, self.mo_sz, self.rb_sz) = \
            struct.unpack_from("<8B", data, 9)
        self.off = 17

    # ---- 基础读取

    def u8(self):
        v = self.data[self.off]
        self.off += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.data, self.off)[0]
        self.off += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.data, self.off)[0]
        self.off += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.data, self.off)[0]
        self.off += 4
        return v

    def vec3(self):
        v = struct.unpack_from("<3f", self.data, self.off)
        self.off += 12
        return v

    def vec4(self):
        v = struct.unpack_from("<4f", self.data, self.off)
        self.off += 16
        return v

    def idx(self, size: int):
        if size == 1:
            return self.u8()
        if size == 2:
            return self.u16()
        return self.u32()

    def string(self):
        n = self.u32()
        raw = self.data[self.off:self.off + n]
        self.off += n
        if self.encoding == 0:
            return raw.decode("utf-16-le", "replace")
        return raw.decode("utf-8", "replace")

    # ---- 各段

    def parse(self) -> Pmx:
        pmx = Pmx()
        pmx.name_jp = self.string()
        pmx.name_en = self.string()
        self.string()  # comment JP
        self.string()  # comment EN
        self._parse_vertices(pmx)
        self._parse_indices(pmx)
        self._parse_textures(pmx)
        self._parse_materials(pmx)
        # 骨骼/变形/刚体：这个导出器的骨骼段与标准 PMX 不兼容
        # （flags 1 字节、尾段布局未知），完整解析仍在攻坚。
        # 渲染 v1 用恒等蒙皮（顶点自带绑定姿势），不需要骨骼 ——
        # 解析失败就留空，不阻塞渲染。
        try:
            self._parse_bones(pmx)
        except Exception as exc:
            pmx.bones = []
            pmx.bone_parse_error = str(exc)[:120]
        try:
            self._parse_morphs(pmx)
        except Exception as exc:
            pmx.morph_names = []
            pmx.morph_counts = {}
            pmx.morph_parse_error = str(exc)[:120]
        return pmx

    def _parse_vertices(self, pmx: Pmx) -> None:
        n = self.u32()
        for _ in range(n):
            v = Vertex()
            v.pos = self.vec3()
            v.normal = self.vec3()
            v.uv = struct.unpack_from("<2f", self.data, self.off)
            self.off += 8
            for _k in range(self.n_uv):
                self.off += 16
            wt = self.u8()
            v.weight_type = wt
            if wt == 0:  # BDEF1
                v.bones = (self.idx(self.bi_sz),)
                v.weights = (1.0,)
            elif wt == 1:  # BDEF2
                b1 = self.idx(self.bi_sz)
                b2 = self.idx(self.bi_sz)
                w1 = self.f32()
                v.bones = (b1, b2)
                v.weights = (w1, 1 - w1)
            elif wt == 2:  # BDEF4
                bs = tuple(self.idx(self.bi_sz) for _ in range(4))
                ws = tuple(self.f32() for _ in range(4))
                v.bones = bs
                v.weights = ws
            elif wt == 3:  # SDEF
                bs = tuple(self.idx(self.bi_sz) for _ in range(4))
                ws = tuple(self.f32() for _ in range(4))
                c = self.vec3()
                r0 = self.vec3()
                r1 = self.vec3()
                v.bones = bs
                v.weights = ws
                v.sdef_c = c
                v.sdef_r0 = r0
                v.sdef_r1 = r1
            # ⚠️ 本模型的导出器在每个权重块后面多写了一个 4 字节填充浮点。
            # 实测（用贴图段偏移反解 + 穷举验证）：跳过它才能对齐到索引段。
            # 若按纯规范解析，第二个顶点的权重类型字节就会读到 140（垃圾）。
            self.off += 4
            pmx.vertices.append(v)

    def _parse_indices(self, pmx: Pmx) -> None:
        n = self.u32()
        pmx.indices = [self.idx(self.vi_sz) for _ in range(n)]

    def _parse_textures(self, pmx: Pmx) -> None:
        n = self.u32()
        pmx.textures = [self.string() for _ in range(n)]

    def _parse_materials(self, pmx: Pmx) -> None:
        n = self.u32()
        for _ in range(n):
            m = Material()
            m.name_jp = self.string()
            m.name_en = self.string()
            m.diffuse = self.vec4()
            m.specular = self.vec3()
            m.spec_strength = self.f32()
            m.ambient = self.vec3()
            m.flags = self.u8()
            m.edge_color = self.vec4()
            m.edge_size = self.f32()
            m.tex_idx = self.idx(self.ti_sz)
            m.sphere_idx = self.idx(self.ti_sz)
            m.sphere_mode = self.u8()
            toon_flag = self.u8()
            m.toon = toon_flag
            m.toon_value = self.u8()
            m.memo = self.string()
            m.face_count = self.u32()
            pmx.materials.append(m)

    def _valid_name_at(self, off: int) -> tuple[bool, int]:
        """off 处是否像一个字符串（长度字段合理 + 全是可见字符）。

        用于这个非标准导出器的骨骼段暴力枚举：骨骼尾段长度未知，
        但下一根骨骼的 JP 名一定是「长度 + UTF-16 可见字符」。
        返回 (是否像, 字符串结束后的偏移)。
        """
        if off + 4 > len(self.data):
            return False, off
        n = struct.unpack_from("<I", self.data, off)[0]
        if not (0 <= n <= 128) or (n % 2) != 0 or off + 4 + n > len(self.data):
            return False, off
        try:
            s = self.data[off + 4:off + 4 + n].decode("utf-16-le")
        except UnicodeDecodeError:
            return False, off
        for ch in s:
            if not (0x20 <= ord(ch) < 0xA000):
                return False, off
        return True, off + 4 + n

    def _parse_bones(self, pmx: Pmx) -> None:
        n = self.u32()
        # 这个导出器的骨骼段有两个非标点：flags 是 1 字节（标准 2 字节），
        # 尾段特殊节布局未知。但**字符串结构是标准的**：
        #   每根骨骼 = JP名（必有）+ EN名（可空，紧跟 JP）+ 固定字段 + 尾段。
        # 实测关键规律：JP 名与前一字符串末尾的间隔 ≥ 22 字节
        # （固定字段 20 + 尾段 ≥ 3，再减去 EN 长度字段……实测 22/32/36 等），
        # 而 EN 名紧跟 JP（间隔 0）。所以「扫出全部字符串，按间隔 ≥22 分类」
        # 能确定性地给出 352 根骨骼的起点，尾段内容完全不用解析。
        anchor = self.data.find("あ".encode("utf-16-le"), self.off)
        if anchor < 0:
            raise ValueError("找不到变形段锚点（「あ」）")
        limit = anchor - 8

        scan: list[tuple[int, int]] = []   # (起点, 名字节数)
        pos = self.off
        while pos < limit:
            if pos + 4 > limit:
                break
            ln = struct.unpack_from("<I", self.data, pos)[0]
            if 1 <= ln <= 128 and ln % 2 == 0 and pos + 4 + ln <= limit:
                try:
                    s = self.data[pos + 4:pos + 4 + ln].decode("utf-16-le")
                    ok = bool(s) and all(0x20 <= ord(ch) < 0xA000 for ch in s)
                except UnicodeDecodeError:
                    ok = False
                if ok:
                    scan.append((pos, ln))
                    pos += 4 + ln
                    continue
            pos += 1

        starts: list[int] = [scan[0][0]] if scan else []
        for j in range(1, len(scan)):
            prev_off, prev_ln = scan[j - 1]
            gap = scan[j][0] - (prev_off + 4 + prev_ln)
            if gap >= 22:
                starts.append(scan[j][0])

        if len(starts) != n:
            raise ValueError(
                f"骨骼起点 {len(starts)} 个 ≠ 声明 {n} 个（格式与预期不符）")

        for i in range(n):
            base = starts[i]
            ln = struct.unpack_from("<I", self.data, base)[0]
            b = Bone()
            b.name_jp = self.data[base + 4:base + 4 + ln].decode(
                "utf-16-le", "replace")
            # 前缀固定字段：EN(4+ln2) + pos12 + parent2 + layer4 + flags1
            o2 = base + 4 + ln
            ln2 = struct.unpack_from("<I", self.data, o2)[0]
            b.name_en = (self.data[o2 + 4:o2 + 4 + ln2].decode(
                "utf-16-le", "replace") if ln2 else "")
            o3 = o2 + 4 + ln2
            b.pos = struct.unpack_from("<3f", self.data, o3)
            b.parent = struct.unpack_from("<h", self.data, o3 + 12)[0]
            b.layer = struct.unpack_from("<I", self.data, o3 + 14)[0]
            b.flags = self.data[o3 + 18]
            pmx.bones.append(b)
        self.off = limit

    def _parse_morphs(self, pmx: Pmx) -> None:
        n = self.u32()
        for _ in range(n):
            name_jp = self.string()
            self.string()
            panel = self.u8()
            mtype = self.u8()
            count = self.u32()
            pmx.morph_names.append(name_jp)
            pmx.morph_counts[name_jp] = count
            if mtype == 0:  # 分组
                for _c in range(count):
                    self.idx(self.mo_sz)
                    self.f32()
            elif mtype == 1:  # 顶点
                for _c in range(count):
                    self.idx(self.vi_sz)
                    self.vec3()
            elif mtype in (3, 4, 5, 6, 7):  # 骨骼/UV/UV2/UV3/UV4/材质
                for _c in range(count):
                    self.idx(2 if mtype == 3 else 4)
                    if mtype == 3:
                        self.vec3()
                        self.vec4()
                    elif mtype in (4, 5):
                        self.vec4()
                    elif mtype == 6:
                        self.vec4()
                    elif mtype == 7:
                        self.u8()
                        self.vec4()
                        self.vec3()
                        self.f32()
                        self.vec4()
                        self.vec4()
                        self.vec4()


def load(path: str) -> Pmx:
    with open(path, "rb") as f:
        data = f.read()
    return Parser(data).parse()
