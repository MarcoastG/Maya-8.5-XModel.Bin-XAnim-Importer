# -*- coding: ascii -*-
# ==============================================================================
# File: bo3_xmodel_bin_importer.py
# Target Path: C:\Program Files (x86)\Autodesk\Maya8.5\bin\plug-ins\bo3_xmodel_bin_importer.py
# Target Software: Autodesk Maya 8.5 (32-bit, Strict Python 2.4 OpenMaya API)
# Version: 3.2 (Clean File Import, World-Space XAnim Keying & Rainbow Skeleton)
# ==============================================================================

import os
import sys
import math
import struct
import traceback

import maya.OpenMaya as OpenMaya
import maya.OpenMayaMPx as OpenMayaMPx
import maya.cmds as cmds
import maya.mel as mel

PLUGIN_NAME = "BO3_XModel_Bin"
TRANSLATOR_NAME = "BO3_XModel_Bin"
CMD_IMPORT_MODEL = "bo3ImportXModel"
CMD_IMPORT_ANIM = "bo3ImportXAnim"
MENU_NAME = "BO3ModToolsMenu"

SUPPORTED_TEX_EXTS = [".tiff", ".tif", ".tga", ".png", ".dds", ".jpg", ".jpeg", ".bmp", ".psd"]

# Maya drawing override colors (1 to 31)
RAINBOW_OVERRIDE_COLORS = [13, 14, 17, 18, 20, 23, 6, 9, 25, 28, 29, 30, 21, 22, 4, 15]


def has_image_extension(filename):
    """Python 2.4 compatible extension check without tuple passing."""
    fn = filename.lower()
    for ext in SUPPORTED_TEX_EXTS:
        if fn.endswith(ext):
            return True
    return False


# ==============================================================================
# 1. PURE PYTHON 2.4 LZ4 DECOMPRESSOR
# ==============================================================================

def to_hex(bs, max_len):
    limit = len(bs)
    if limit > max_len:
        limit = max_len
    parts = []
    for i in range(limit):
        parts.append("%02X" % ord(bs[i]))
    return " ".join(parts)


def lz4_uncompress_stream(comp_bytes, uncompressed_size):
    src_len = len(comp_bytes)
    pos = 0
    out_chars = []

    while pos < src_len and len(out_chars) < uncompressed_size:
        token = ord(comp_bytes[pos])
        pos = pos + 1

        lit_len = (token >> 4) & 0x0F
        if lit_len == 15:
            while pos < src_len:
                b = ord(comp_bytes[pos])
                pos = pos + 1
                lit_len = lit_len + b
                if b != 255:
                    break

        if lit_len > 0:
            end_lit = pos + lit_len
            if end_lit > src_len:
                end_lit = src_len
            out_chars.extend(list(comp_bytes[pos:end_lit]))
            pos = end_lit

        if pos >= src_len or len(out_chars) >= uncompressed_size:
            break

        if pos + 2 > src_len:
            break
        match_offset = ord(comp_bytes[pos]) | (ord(comp_bytes[pos + 1]) << 8)
        pos = pos + 2
        if match_offset == 0:
            break

        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while pos < src_len:
                b = ord(comp_bytes[pos])
                pos = pos + 1
                match_len = match_len + b
                if b != 255:
                    break

        start_idx = len(out_chars) - match_offset
        if start_idx < 0:
            break
        if match_len <= match_offset:
            out_chars.extend(out_chars[start_idx:start_idx + match_len])
        else:
            for i in range(match_len):
                out_chars.append(out_chars[start_idx + i])

    if len(out_chars) > uncompressed_size:
        out_chars = out_chars[:uncompressed_size]
    return "".join(out_chars)


def is_valid_xbin_payload(buf):
    if len(buf) < 8:
        return False
    first_u16 = struct.unpack("<H", buf[:2])[0]
    if first_u16 in (0xC355, 0x432F, 0x4642, 0x2F2F, 0x2456, 0x24D1):
        return True
    head = buf[:64].lstrip()
    if (
        head.startswith("//") or
        head.startswith("MODEL") or
        head.startswith("VERSION") or
        head.startswith("ANIMATION")
    ):
        return True
    return False


def unpack_xbin_lz4(raw_bytes):
    if len(raw_bytes) < 8:
        return raw_bytes

    if is_valid_xbin_payload(raw_bytes):
        return raw_bytes

    if raw_bytes[:5] in ("*LZ4*", "$LZ4$") and len(raw_bytes) > 9:
        u_size = struct.unpack("<I", raw_bytes[5:9])[0]
        if 16 < u_size < 350000000:
            decomp = lz4_uncompress_stream(raw_bytes[9:], u_size)
            if len(decomp) > 0:
                return decomp

    for pair in ((4, 8), (5, 9), (4, 12), (8, 12), (8, 16), (0, 4), (12, 16)):
        u_off = pair[0]
        s_off = pair[1]
        if s_off < len(raw_bytes) and u_off + 4 <= s_off:
            u_size = struct.unpack("<I", raw_bytes[u_off:u_off + 4])[0]
            if 32 < u_size < 350000000:
                try:
                    decomp = lz4_uncompress_stream(raw_bytes[s_off:], u_size)
                    if is_valid_xbin_payload(decomp) or len(decomp) == u_size:
                        return decomp
                except Exception:
                    pass

    return raw_bytes


# ==============================================================================
# 2. DATA STRUCTURES (XModel & XAnim)
# ==============================================================================

class XModelBone:
    def __init__(self, index, name, parent, offset, matrix):
        self.index = index
        self.name = name
        self.parent = parent
        self.offset = offset
        self.matrix = matrix


class XModelVert:
    def __init__(self, offset, weights):
        self.offset = offset
        self.weights = weights


class XModelFace:
    def __init__(self, object_id, material_id, indices, normals, colors, uvs):
        self.object_id = object_id
        self.material_id = material_id
        self.indices = indices
        self.normals = normals
        self.colors = colors
        self.uvs = uvs


class XModelMaterial:
    def __init__(self, index, name, mat_type, color_map):
        self.index = index
        self.name = name
        self.mat_type = mat_type
        self.color_map = color_map


class XModelAsset:
    def __init__(self):
        self.version = 7
        self.bones = []
        self.verts = []
        self.faces = []
        self.objects = []
        self.materials = []


class XAnimFramePart:
    def __init__(self, part_index, offset, matrix):
        self.part_index = part_index
        self.offset = offset
        self.matrix = matrix


class XAnimFrame:
    def __init__(self, frame_num, parts):
        self.frame_num = frame_num
        self.parts = parts


class XAnimAsset:
    def __init__(self):
        self.version = 3
        self.framerate = 30.0
        self.num_frames = 0
        self.parts = []
        self.frames = []


# ==============================================================================
# 3. BINARY READER
# ==============================================================================

OP_COMMENT = 0xC355
OP_OFFSET  = 0x9383
OP_SCALE   = 0x1B6D
OP_X_SET   = (0xDC38, 0x38DC)
OP_Y_SET   = (0x0CDC, 0xCC35, 0xDC0C)
OP_Z_SET   = (0xFCBF, 0xFC4E, 0xBFFC)


class BinaryReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.length = len(data)

    def remaining(self):
        return self.length - self.pos

    def eof(self):
        return self.pos >= self.length

    def align4(self):
        rem = self.pos & 3
        if rem != 0:
            self.pos = self.pos + (4 - rem)

    def peek_u16(self):
        if self.pos + 2 > self.length:
            return 0
        return struct.unpack("<H", self.data[self.pos:self.pos + 2])[0]

    def peek_u16_at(self, offset):
        if offset + 2 > self.length:
            return 0
        return struct.unpack("<H", self.data[offset:offset + 2])[0]

    def peek_i16_at(self, offset):
        if offset + 2 > self.length:
            return 0
        return struct.unpack("<h", self.data[offset:offset + 2])[0]

    def peek_u32_at(self, offset):
        if offset + 4 > self.length:
            return 0
        return struct.unpack("<I", self.data[offset:offset + 4])[0]

    def read_u8(self):
        if self.pos + 1 > self.length:
            self.pos = self.length
            return 0
        val = ord(self.data[self.pos])
        self.pos = self.pos + 1
        return val

    def read_u16(self):
        if self.pos + 2 > self.length:
            self.pos = self.length
            return 0
        val = struct.unpack("<H", self.data[self.pos:self.pos + 2])[0]
        self.pos = self.pos + 2
        return val

    def read_i16(self):
        if self.pos + 2 > self.length:
            self.pos = self.length
            return 0
        val = struct.unpack("<h", self.data[self.pos:self.pos + 2])[0]
        self.pos = self.pos + 2
        return val

    def read_u32(self):
        if self.pos + 4 > self.length:
            self.pos = self.length
            return 0
        val = struct.unpack("<I", self.data[self.pos:self.pos + 4])[0]
        self.pos = self.pos + 4
        return val

    def read_i32(self):
        if self.pos + 4 > self.length:
            self.pos = self.length
            return -1
        val = struct.unpack("<i", self.data[self.pos:self.pos + 4])[0]
        self.pos = self.pos + 4
        return val

    def read_f32(self):
        if self.pos + 4 > self.length:
            self.pos = self.length
            return 0.0
        val = struct.unpack("<f", self.data[self.pos:self.pos + 4])[0]
        self.pos = self.pos + 4
        return val

    def read_vec2(self):
        if self.pos + 8 > self.length:
            self.pos = self.length
            return (0.0, 0.0)
        val = struct.unpack("<2f", self.data[self.pos:self.pos + 8])
        self.pos = self.pos + 8
        return val

    def read_vec3(self):
        if self.pos + 12 > self.length:
            self.pos = self.length
            return (0.0, 0.0, 0.0)
        val = struct.unpack("<3f", self.data[self.pos:self.pos + 12])
        self.pos = self.pos + 12
        return val

    def read_i16_vec3(self):
        if self.pos + 6 > self.length:
            self.pos = self.length
            return (0.0, 0.0, 0.0)
        ix, iy, iz = struct.unpack("<3h", self.data[self.pos:self.pos + 6])
        self.pos = self.pos + 6
        return (ix / 32767.0, iy / 32767.0, iz / 32767.0)

    def read_cstring_aligned(self):
        if self.pos >= self.length:
            return ""
        end = self.data.find("\x00", self.pos)
        if end == -1:
            s = self.data[self.pos:]
            self.pos = self.length
            return s
        s = self.data[self.pos:end]
        self.pos = end + 1
        self.align4()
        return s


def skip_comments(reader, op_comment_set):
    while not reader.eof():
        reader.align4()
        if reader.remaining() < 4:
            break
        op = reader.peek_u16()
        if op in op_comment_set:
            reader.read_u16()
            reader.read_u16()
            reader.read_cstring_aligned()
        else:
            break


def is_printable_cstring(data, start_pos):
    if start_pos >= len(data):
        return False
    end = data.find("\x00", start_pos)
    if end == -1 or end == start_pos or (end - start_pos) > 256:
        return False
    for idx in range(start_pos, end):
        ch = ord(data[idx])
        if ch < 32 or ch > 126:
            return False
    return True


# ==============================================================================
# 4. XMODEL PARSING
# ==============================================================================

def parse_xmodel_bin_stream(raw_bytes):
    decompressed = unpack_xbin_lz4(raw_bytes)

    if decompressed.lstrip().startswith("//") or "MODEL" in decompressed[:128]:
        return parse_xmodel_export_text(decompressed)

    reader = BinaryReader(decompressed)
    asset = XModelAsset()

    op_comments = [0xC355, 0x432F]
    first_op = reader.peek_u16()
    if first_op not in op_comments and is_printable_cstring(decompressed, 4):
        op_comments.append(first_op)

    skip_comments(reader, op_comments)

    while not reader.eof():
        skip_comments(reader, op_comments)
        if reader.remaining() < 4:
            break
        op = reader.read_u16()
        arg = reader.read_u16()
        if 5 <= arg <= 8:
            asset.version = arg
            break

    skip_comments(reader, op_comments)
    if reader.remaining() < 4:
        return asset

    op_numbones = reader.read_u16()
    num_bones = reader.read_u16()

    skip_comments(reader, op_comments)
    if reader.remaining() >= 8:
        peek_arg = reader.peek_u16_at(reader.pos + 2)
        peek_parent = reader.peek_i16_at(reader.pos + 4)
        if not (peek_arg == 0 and peek_parent == -1):
            _op_cosmetic = reader.read_u16()
            _cosmetic_count = reader.read_u16()
            skip_comments(reader, op_comments)

    bone_decl_map = {}
    for b_i in range(num_bones):
        skip_comments(reader, op_comments)
        if reader.remaining() < 8:
            break
        _op_bdecl = reader.read_u16()
        pos_after_op = reader.pos
        if is_printable_cstring(decompressed, pos_after_op + 6):
            b_idx = reader.read_u16()
            b_parent = reader.read_i32()
        elif is_printable_cstring(decompressed, pos_after_op + 10):
            reader.read_u16()
            b_idx = reader.read_i32()
            b_parent = reader.read_i32()
        elif is_printable_cstring(decompressed, pos_after_op + 4):
            b_idx = reader.read_u16()
            b_parent = reader.read_i16()
        else:
            b_idx = reader.read_u16()
            b_parent = reader.read_i32()

        b_name = reader.read_cstring_aligned()
        bone_decl_map[b_i] = (b_name, b_parent)

    for b_i in range(num_bones):
        skip_comments(reader, op_comments)
        if reader.remaining() < 44:
            break

        _btrans_op = reader.read_u16()
        b_idx = reader.read_u16()

        skip_comments(reader, op_comments)
        _op_off = reader.read_u16()
        reader.read_u16()
        offset_vec = reader.read_vec3()

        skip_comments(reader, op_comments)
        if reader.peek_u16() not in OP_X_SET:
            _op_scale = reader.read_u16()
            reader.read_u16()
            _scale_vec = reader.read_vec3()

        skip_comments(reader, op_comments)
        _op_x = reader.read_u16()
        mat_x = reader.read_i16_vec3()

        skip_comments(reader, op_comments)
        _op_y = reader.read_u16()
        mat_y = reader.read_i16_vec3()

        skip_comments(reader, op_comments)
        _op_z = reader.read_u16()
        mat_z = reader.read_i16_vec3()

        b_name, b_parent = bone_decl_map.get(b_i, ("bone_%d" % b_i, -1))
        asset.bones.append(XModelBone(b_i, b_name, b_parent, offset_vec, [mat_x, mat_y, mat_z]))

    skip_comments(reader, op_comments)
    if reader.remaining() < 4:
        return asset

    op_numverts = reader.read_u16()
    num_verts = reader.read_u16()
    is_vert32 = False
    if num_verts == 0 and reader.remaining() >= 4:
        num_verts = reader.read_u32()
        is_vert32 = True

    op_vert = None
    op_bones_cnt = None
    op_boneweight = None
    bw_is_u16 = None

    for v_i in range(num_verts):
        skip_comments(reader, op_comments)
        if reader.remaining() < 20:
            break

        v_op = reader.read_u16()
        if op_vert is None:
            op_vert = v_op
        _v_arg = reader.read_u16()
        if reader.peek_u16() != OP_OFFSET and reader.peek_u16_at(reader.pos + 4) == OP_OFFSET:
            _v_idx32 = reader.read_u32()
            is_vert32 = True

        off_op = reader.read_u16()
        reader.read_u16()
        v_offset = reader.read_vec3()

        bones_op = reader.read_u16()
        if op_bones_cnt is None:
            op_bones_cnt = bones_op
        num_weights = reader.read_u16()

        weights = []
        for _w_i in range(num_weights):
            bw_op = reader.read_u16()
            if op_boneweight is None:
                op_boneweight = bw_op
            w_bone = reader.read_u16()

            if bw_is_u16 is None:
                next_at_2 = reader.peek_u16_at(reader.pos + 2)
                next_at_4 = reader.peek_u16_at(reader.pos + 4)
                if next_at_2 in (op_vert, op_boneweight):
                    bw_is_u16 = True
                elif next_at_4 in (op_vert, op_boneweight):
                    bw_is_u16 = False
                else:
                    bw_is_u16 = True

            if bw_is_u16:
                w_val = reader.read_u16() / 65535.0
            else:
                w_val = reader.read_f32()
            weights.append((w_bone, w_val))

        asset.verts.append(XModelVert(v_offset, weights))

    skip_comments(reader, op_comments)
    if reader.remaining() < 4:
        return asset

    op_numfaces = reader.read_u16()
    num_faces = reader.read_u16()
    if num_faces == 0 and reader.remaining() >= 4:
        num_faces = reader.read_u32()

    op_tri = None
    op_normal = None
    op_uv = None
    corner_stride = None
    tri_hdr_extra = None

    for f_i in range(num_faces):
        skip_comments(reader, op_comments)
        if reader.remaining() < 32:
            break

        t_op = reader.read_u16()
        if op_tri is None:
            op_tri = t_op

        if tri_hdr_extra is None:
            for cand_h in (2, 4, 6, 8):
                if reader.peek_u16_at(reader.pos + cand_h) == op_vert:
                    tri_hdr_extra = cand_h
                    break
            if tri_hdr_extra is None:
                tri_hdr_extra = 2

        if tri_hdr_extra == 2:
            obj_id = reader.read_u8()
            mat_id = reader.read_u8()
        elif tri_hdr_extra == 4:
            obj_id = reader.read_u16()
            mat_id = reader.read_u16()
        elif tri_hdr_extra == 6:
            obj_id = reader.read_u16()
            mat_id = reader.read_u16()
            reader.read_u16()
        else:
            obj_id = reader.read_u16()
            mat_id = reader.read_u16()
            reader.read_u32()

        f_indices = []
        f_normals = []
        f_colors = []
        f_uvs = []

        for _corner in range(3):
            corner_start = reader.pos

            if corner_stride is None:
                for cand_s in range(16, 68, 2):
                    if reader.peek_u16_at(corner_start + cand_s) == op_vert:
                        corner_stride = cand_s
                        break
                if corner_stride is None:
                    corner_stride = 32

            _c_vop = reader.read_u16()
            c_varg = reader.read_u16()
            if is_vert32 and corner_stride in (28, 36, 40, 48, 56):
                v_idx = reader.read_u32()
            else:
                v_idx = c_varg

            vert_hdr_len = reader.pos - corner_start
            rem_corner = corner_stride - vert_hdr_len

            norm = (0.0, 0.0, 1.0)
            col = (1.0, 1.0, 1.0, 1.0)
            uv = (0.0, 0.0)

            if rem_corner >= 20:
                n_op = reader.read_u16()
                if op_normal is None:
                    op_normal = n_op

                if rem_corner in (20, 28, 40):
                    norm = reader.read_i16_vec3()
                else:
                    reader.read_u16()
                    norm = reader.read_vec3()

                reader.pos = corner_start + corner_stride - 12
                uv_op = reader.read_u16()
                if op_uv is None:
                    op_uv = uv_op
                _uv_cnt = reader.read_u16()
                uv = reader.read_vec2()
            else:
                reader.pos = corner_start + corner_stride

            f_indices.append(v_idx)
            f_normals.append(norm)
            f_colors.append(col)
            f_uvs.append(uv)

        asset.faces.append(XModelFace(obj_id, mat_id, f_indices, f_normals, f_colors, f_uvs))

    skip_comments(reader, op_comments)
    if reader.remaining() >= 4:
        _op_numobjs = reader.read_u16()
        num_objs = reader.read_u16()
        if 0 < num_objs < 4096:
            for _o_i in range(num_objs):
                skip_comments(reader, op_comments)
                if reader.remaining() < 4:
                    break
                _op_obj = reader.read_u16()
                _obj_idx = reader.read_u16()
                o_name = reader.read_cstring_aligned()
                asset.objects.append(o_name)

    skip_comments(reader, op_comments)
    if reader.remaining() >= 4:
        _op_nummats = reader.read_u16()
        num_mats = reader.read_u16()
        if 0 < num_mats < 4096:
            op_mat = None
            for m_i in range(num_mats):
                skip_comments(reader, op_comments)
                if reader.remaining() < 4:
                    break
                cur_op = reader.read_u16()
                if op_mat is None:
                    op_mat = cur_op
                m_idx = reader.read_u16()
                m_name = reader.read_cstring_aligned()
                m_type = reader.read_cstring_aligned()
                m_cmap = reader.read_cstring_aligned()

                while not reader.eof():
                    reader.align4()
                    if reader.remaining() < 4:
                        break
                    peek_op = reader.peek_u16()
                    peek_idx = reader.peek_u16_at(reader.pos + 2)
                    if (op_mat is not None and peek_op == op_mat and peek_idx == m_i + 1) or peek_op in op_comments:
                        break
                    reader.read_u32()

                asset.materials.append(XModelMaterial(m_idx, m_name, m_type, m_cmap))

    return asset


def parse_xmodel_export_text(text):
    asset = XModelAsset()
    lines = text.splitlines()
    n = len(lines)
    i = 0
    bone_info_map = {}

    while i < n:
        line = lines[i].strip()
        i = i + 1
        if not line or line.startswith("//"):
            continue

        tokens = line.split()
        key = tokens[0]

        if key == "VERSION":
            asset.version = int(tokens[1])

        elif key == "NUMBONES":
            num_bones = int(tokens[1])
            for _b in range(num_bones):
                while i < n:
                    bline = lines[i].strip()
                    i = i + 1
                    if bline and not bline.startswith("//"):
                        first_q = bline.find('"')
                        last_q = bline.rfind('"')
                        bname = bline[first_q + 1:last_q]
                        btoks = bline[:first_q].split()
                        b_idx = int(btoks[1])
                        b_parent = int(btoks[2])
                        bone_info_map[b_idx] = (bname, b_parent)
                        break

        elif key == "BONE":
            b_idx = int(tokens[1])
            offset = (0.0, 0.0, 0.0)
            mat_x = (1.0, 0.0, 0.0)
            mat_y = (0.0, 1.0, 0.0)
            mat_z = (0.0, 0.0, 1.0)

            while i < n:
                tline = lines[i].strip()
                if not tline or tline.startswith("//"):
                    i = i + 1
                    continue
                ttoks = tline.split()
                tkey = ttoks[0].rstrip(":")
                if tkey == "OFFSET":
                    offset = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                elif tkey == "SCALE":
                    i = i + 1
                elif tkey == "X":
                    mat_x = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                elif tkey == "Y":
                    mat_y = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                elif tkey == "Z":
                    mat_z = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                    break
                else:
                    break

            bname, bparent = bone_info_map.get(b_idx, ("bone_%d" % b_idx, -1))
            asset.bones.append(XModelBone(b_idx, bname, bparent, offset, [mat_x, mat_y, mat_z]))

        elif key in ("VERT", "VERT32"):
            v_offset = (0.0, 0.0, 0.0)
            weights = []
            while i < n:
                vline = lines[i].strip()
                if not vline or vline.startswith("//"):
                    i = i + 1
                    continue
                vtoks = vline.split()
                vkey = vtoks[0].rstrip(":")
                if vkey == "OFFSET":
                    v_offset = (
                        float(vtoks[1].rstrip(",")),
                        float(vtoks[2].rstrip(",")),
                        float(vtoks[3].rstrip(","))
                    )
                    i = i + 1
                elif vkey == "BONES":
                    nb = int(vtoks[1])
                    i = i + 1
                    for _w in range(nb):
                        while i < n:
                            wline = lines[i].strip()
                            i = i + 1
                            if wline and not wline.startswith("//"):
                                wtoks = wline.split()
                                weights.append((int(wtoks[1]), float(wtoks[2])))
                                break
                    break
                else:
                    break
            asset.verts.append(XModelVert(v_offset, weights))

        elif key in ("TRI", "TRI16"):
            obj_id = int(tokens[1])
            mat_id = int(tokens[2])
            f_indices = []
            f_normals = []
            f_colors = []
            f_uvs = []
            verts_read = 0
            while i < n and verts_read < 3:
                fline = lines[i].strip()
                if not fline or fline.startswith("//"):
                    i = i + 1
                    continue
                ftoks = fline.split()
                fkey = ftoks[0].rstrip(":")
                if fkey in ("VERT", "VERT32"):
                    f_indices.append(int(ftoks[1]))
                    i = i + 1
                    norm = (0.0, 0.0, 1.0)
                    col = (1.0, 1.0, 1.0, 1.0)
                    uv = (0.0, 0.0)
                    while i < n:
                        sline = lines[i].strip()
                        if not sline or sline.startswith("//"):
                            i = i + 1
                            continue
                        stoks = sline.split()
                        skey = stoks[0].rstrip(":")
                        if skey == "NORMAL":
                            norm = (float(stoks[1]), float(stoks[2]), float(stoks[3]))
                            i = i + 1
                        elif skey == "COLOR":
                            col = (float(stoks[1]), float(stoks[2]), float(stoks[3]), float(stoks[4]))
                            i = i + 1
                        elif skey == "UV":
                            if len(stoks) >= 4:
                                uv = (float(stoks[2]), float(stoks[3]))
                            elif len(stoks) >= 3:
                                uv = (float(stoks[1]), float(stoks[2]))
                            i = i + 1
                            break
                        else:
                            break
                    f_normals.append(norm)
                    f_colors.append(col)
                    f_uvs.append(uv)
                    verts_read = verts_read + 1
                else:
                    i = i + 1
            if len(f_indices) == 3:
                asset.faces.append(XModelFace(obj_id, mat_id, f_indices, f_normals, f_colors, f_uvs))

        elif key == "OBJECT":
            first_q = line.find('"')
            last_q = line.rfind('"')
            if first_q != -1 and last_q > first_q:
                asset.objects.append(line[first_q + 1:last_q])
            else:
                asset.objects.append("mesh_%d" % len(asset.objects))

        elif key == "MATERIAL":
            quotes = []
            qpos = 0
            while True:
                q1 = line.find('"', qpos)
                if q1 == -1:
                    break
                q2 = line.find('"', q1 + 1)
                if q2 == -1:
                    break
                quotes.append(line[q1 + 1:q2])
                qpos = q2 + 1
            m_idx = int(tokens[1])
            if len(quotes) > 0:
                m_name = quotes[0]
            else:
                m_name = "material_%d" % m_idx

            if len(quotes) > 1:
                m_type = quotes[1]
            else:
                m_type = "Phong"

            if len(quotes) > 2:
                m_cmap = quotes[2]
            else:
                m_cmap = ""

            asset.materials.append(XModelMaterial(m_idx, m_name, m_type, m_cmap))

    return asset


def load_xmodel_file(filepath):
    f = open(filepath, "rb")
    try:
        raw_bytes = f.read()
    finally:
        f.close()

    header_str = raw_bytes[:128].lstrip()
    if header_str.startswith("//") or header_str.startswith("MODEL"):
        return parse_xmodel_export_text(raw_bytes)

    return parse_xmodel_bin_stream(raw_bytes)


# ==============================================================================
# 5. XANIM PARSING (.xanim_bin and .xanim_export)
# ==============================================================================

def parse_xanim_export_text(text):
    asset = XAnimAsset()
    lines = text.splitlines()
    n = len(lines)
    i = 0
    cur_frame = None

    while i < n:
        line = lines[i].strip()
        i = i + 1
        if not line or line.startswith("//"):
            continue

        tokens = line.split()
        key = tokens[0]

        if key == "VERSION":
            asset.version = int(tokens[1])

        elif key == "NUMPARTS":
            num_parts = int(tokens[1])
            for _p in range(num_parts):
                while i < n:
                    pline = lines[i].strip()
                    i = i + 1
                    if pline and not pline.startswith("//"):
                        first_q = pline.find('"')
                        last_q = pline.rfind('"')
                        pname = pline[first_q + 1:last_q]
                        ptoks = pline[:first_q].split()
                        p_idx = int(ptoks[1])
                        asset.parts.append((p_idx, pname))
                        break

        elif key == "FRAMERATE":
            asset.framerate = float(tokens[1])

        elif key == "NUMFRAMES":
            asset.num_frames = int(tokens[1])

        elif key == "FRAME":
            f_num = int(tokens[1])
            cur_frame = XAnimFrame(f_num, [])
            asset.frames.append(cur_frame)

        elif key == "PART":
            p_idx = int(tokens[1])
            offset = (0.0, 0.0, 0.0)
            mat_x = (1.0, 0.0, 0.0)
            mat_y = (0.0, 1.0, 0.0)
            mat_z = (0.0, 0.0, 1.0)

            while i < n:
                tline = lines[i].strip()
                if not tline or tline.startswith("//"):
                    i = i + 1
                    continue
                ttoks = tline.split()
                tkey = ttoks[0].rstrip(":")

                if tkey == "OFFSET":
                    offset = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                elif tkey == "SCALE":
                    i = i + 1
                elif tkey == "X":
                    mat_x = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                elif tkey == "Y":
                    mat_y = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                elif tkey == "Z":
                    mat_z = (
                        float(ttoks[1].rstrip(",")),
                        float(ttoks[2].rstrip(",")),
                        float(ttoks[3].rstrip(","))
                    )
                    i = i + 1
                    break
                else:
                    break

            if cur_frame is not None:
                cur_frame.parts.append(XAnimFramePart(p_idx, offset, [mat_x, mat_y, mat_z]))

        elif key == "NOTETRACKS":
            break

    return asset


def parse_xanim_bin_stream(raw_bytes):
    decompressed = unpack_xbin_lz4(raw_bytes)

    if decompressed.lstrip().startswith("//") or "ANIMATION" in decompressed[:128]:
        return parse_xanim_export_text(decompressed)

    reader = BinaryReader(decompressed)
    asset = XAnimAsset()

    op_comments = [0xC355, 0x432F]
    first_op = reader.peek_u16()
    if first_op not in op_comments and is_printable_cstring(decompressed, 4):
        op_comments.append(first_op)

    skip_comments(reader, op_comments)

    while not reader.eof():
        skip_comments(reader, op_comments)
        if reader.remaining() < 4:
            break
        op = reader.read_u16()
        arg = reader.read_u16()
        if 1 <= arg <= 8:
            asset.version = arg
            break

    skip_comments(reader, op_comments)
    if reader.remaining() < 4:
        return asset

    _op_numparts = reader.read_u16()
    num_parts = reader.read_u16()

    for p_i in range(num_parts):
        skip_comments(reader, op_comments)
        if reader.remaining() < 4:
            break
        _op_p = reader.read_u16()
        p_idx = reader.read_u16()
        p_name = reader.read_cstring_aligned()
        asset.parts.append((p_idx, p_name))

    skip_comments(reader, op_comments)

    if reader.remaining() >= 8:
        _op_fps = reader.read_u16()
        fps_val = reader.read_f32()
        if fps_val <= 0.0 or fps_val > 240.0:
            fps_val = 30.0
        asset.framerate = fps_val

        skip_comments(reader, op_comments)
        _op_nframes = reader.read_u16()
        num_frames = reader.read_u16()
        if num_frames == 0 and reader.remaining() >= 4:
            num_frames = reader.read_u32()
        asset.num_frames = num_frames

    while not reader.eof():
        skip_comments(reader, op_comments)
        if reader.remaining() < 4:
            break

        _f_op = reader.read_u16()
        f_idx = reader.read_u16()
        cur_frame = XAnimFrame(f_idx, [])

        for p_i in range(len(asset.parts)):
            skip_comments(reader, op_comments)
            if reader.remaining() < 28:
                break

            _op_p = reader.read_u16()
            p_idx = reader.read_u16()

            skip_comments(reader, op_comments)
            _op_off = reader.read_u16()
            reader.read_u16()
            offset_vec = reader.read_vec3()

            skip_comments(reader, op_comments)
            if reader.peek_u16() not in OP_X_SET:
                _op_scale = reader.read_u16()
                reader.read_u16()
                _scale_vec = reader.read_vec3()

            skip_comments(reader, op_comments)
            _op_x = reader.read_u16()
            # Adaptive int16 vs float rotation vector read
            if (
                reader.peek_u16_at(reader.pos + 6) in OP_Y_SET or
                reader.peek_u16_at(reader.pos + 8) in OP_Y_SET
            ):
                mat_x = reader.read_i16_vec3()
                skip_comments(reader, op_comments)
                _op_y = reader.read_u16()
                mat_y = reader.read_i16_vec3()
                skip_comments(reader, op_comments)
                _op_z = reader.read_u16()
                mat_z = reader.read_i16_vec3()
            else:
                if reader.peek_u16_at(reader.pos + 2) == 0 and reader.peek_u16_at(reader.pos + 16) in OP_Y_SET:
                    reader.read_u16()
                mat_x = reader.read_vec3()
                skip_comments(reader, op_comments)
                _op_y = reader.read_u16()
                if reader.peek_u16_at(reader.pos + 2) == 0 and reader.peek_u16_at(reader.pos + 16) in OP_Z_SET:
                    reader.read_u16()
                mat_y = reader.read_vec3()
                skip_comments(reader, op_comments)
                _op_z = reader.read_u16()
                if reader.peek_u16_at(reader.pos + 2) == 0:
                    reader.read_u16()
                mat_z = reader.read_vec3()

            cur_frame.parts.append(XAnimFramePart(p_idx, offset_vec, [mat_x, mat_y, mat_z]))

        asset.frames.append(cur_frame)

    return asset


def load_xanim_file(filepath):
    f = open(filepath, "rb")
    try:
        raw_bytes = f.read()
    finally:
        f.close()

    header_str = raw_bytes[:128].lstrip()
    if header_str.startswith("//") or header_str.startswith("ANIMATION"):
        return parse_xanim_export_text(raw_bytes)

    return parse_xanim_bin_stream(raw_bytes)


# ==============================================================================
# 6. TEXTURE LOCATOR & SCENE BUILDER
# ==============================================================================

def sanitize_maya_name(name):
    clean = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            clean.append(ch)
        else:
            clean.append("_")
    res = "".join(clean)
    if not res or res[0].isdigit():
        res = "m_" + res
    return res


def find_material_texture(model_dir, mat, model_name):
    if not model_dir or not os.path.isdir(model_dir):
        return None

    search_dirs = [
        model_dir,
        os.path.join(model_dir, "textures"),
        os.path.join(model_dir, "Textures"),
        os.path.join(model_dir, "_textures"),
        os.path.join(model_dir, "images")
    ]

    candidate_stems = []

    if mat and mat.color_map:
        cmap = mat.color_map.replace("\\", "/").strip()
        cmap_base = os.path.basename(cmap)
        cmap_stem = os.path.splitext(cmap_base)[0]
        if cmap_base:
            candidate_stems.append(cmap_base.lower())
        if cmap_stem:
            candidate_stems.append(cmap_stem.lower())

    if mat and mat.name:
        mname = mat.name.strip().lower()
        candidate_stems.append(mname)
        candidate_stems.append(mname + "_c")
        candidate_stems.append(mname + "_color")
        candidate_stems.append(mname + "_col")
        candidate_stems.append(mname + "_diffuse")
        candidate_stems.append(mname + "_albedo")
        if mname.endswith("_mat"):
            mname_no_mat = mname[:-4]
            candidate_stems.append(mname_no_mat)
            candidate_stems.append(mname_no_mat + "_c")
            candidate_stems.append(mname_no_mat + "_color")

    if model_name:
        mod = model_name.strip().lower()
        candidate_stems.append(mod)
        candidate_stems.append(mod + "_c")
        candidate_stems.append(mod + "_color")
        candidate_stems.append(mod + "_diffuse")
        candidate_stems.append(mod + "_mat_c")
        candidate_stems.append(mod + "_mat")

    for sdir in search_dirs:
        if not os.path.isdir(sdir):
            continue
        try:
            files_in_dir = os.listdir(sdir)
        except Exception:
            continue

        for cand in candidate_stems:
            for ext in SUPPORTED_TEX_EXTS:
                if cand.endswith(ext):
                    exact_name = cand
                else:
                    exact_name = cand + ext

                for f in files_in_dir:
                    if f.lower() == exact_name:
                        return os.path.normpath(os.path.join(sdir, f))

    for sdir in search_dirs:
        if not os.path.isdir(sdir):
            continue
        try:
            files_in_dir = os.listdir(sdir)
        except Exception:
            continue

        for f in files_in_dir:
            f_lower = f.lower()
            if not has_image_extension(f_lower):
                continue
            stem = os.path.splitext(f_lower)[0]
            if mat and mat.name and mat.name.lower() in stem:
                return os.path.normpath(os.path.join(sdir, f))
            if model_name and model_name.lower() in stem:
                return os.path.normpath(os.path.join(sdir, f))

    for sdir in search_dirs:
        if not os.path.isdir(sdir):
            continue
        try:
            image_files = []
            for f in os.listdir(sdir):
                if has_image_extension(f):
                    image_files.append(f)
            if len(image_files) == 1:
                return os.path.normpath(os.path.join(sdir, image_files[0]))
        except Exception:
            pass

    return None


def enable_smooth_shaded_viewports():
    try:
        if cmds.about(batch=True):
            return
        panels = cmds.getPanel(type="modelPanel")
        if panels:
            for p in panels:
                try:
                    cmds.modelEditor(
                        p,
                        edit=True,
                        displayAppearance="smoothShaded",
                        displayTextures=True,
                        displayLights="default"
                    )
                except Exception:
                    pass
    except Exception:
        pass


def build_maya_scene(asset, model_name, model_dir=""):
    cmds.select(clear=True)

    # 1. Create Materials, Shading Groups & File Textures
    shading_groups = {}
    for mat in asset.materials:
        mat_name = sanitize_maya_name(mat.name)
        shader = cmds.shadingNode("lambert", asShader=True, name=mat_name)
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=mat_name + "SG")
        cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
        shading_groups[mat.index] = sg

        tex_path = find_material_texture(model_dir, mat, model_name)
        if tex_path and os.path.isfile(tex_path):
            try:
                clean_tex_path = tex_path.replace("\\", "/")
                file_node = cmds.shadingNode("file", asTexture=True, name=mat_name + "_file")
                place_node = cmds.shadingNode("place2dTexture", asUtility=True, name=mat_name + "_place2d")

                # Connect universal 2D attributes verified on Maya 8.5
                p2d_attrs = [
                    "coverage", "translateFrame", "rotateFrame",
                    "mirrorU", "mirrorV", "stagger", "wrapU", "wrapV",
                    "repeatUV", "offset", "rotateUV", "noiseUV"
                ]
                for attr in p2d_attrs:
                    try:
                        cmds.connectAttr(place_node + "." + attr, file_node + "." + attr, force=True)
                    except Exception:
                        pass

                try:
                    cmds.connectAttr(place_node + ".outUV", file_node + ".uvCoord", force=True)
                except Exception:
                    pass
                try:
                    cmds.connectAttr(place_node + ".outUvFilterSize", file_node + ".uvFilterSize", force=True)
                except Exception:
                    pass

                cmds.setAttr(file_node + ".fileTextureName", clean_tex_path, type="string")
                cmds.connectAttr(file_node + ".outColor", shader + ".color", force=True)
            except Exception:
                pass

    # 2. Create Bone Hierarchy with Rainbow Colors & Exact World Matrices
    joint_names = []
    for b_i in range(len(asset.bones)):
        bone = asset.bones[b_i]
        cmds.select(clear=True)
        j_name = sanitize_maya_name(bone.name)
        if cmds.objExists(j_name):
            j_name = model_name + "_" + j_name

        if 0 <= bone.parent < len(joint_names):
            cmds.select(joint_names[bone.parent], replace=True)

        jnt = cmds.joint(name=j_name, radius=0.5)
        cmds.setAttr(jnt + ".jointOrient", 0, 0, 0)

        # Rainbow colorization (safe boolean 1 for useObjectColor)
        col_idx = RAINBOW_OVERRIDE_COLORS[b_i % len(RAINBOW_OVERRIDE_COLORS)]
        try:
            cmds.setAttr(jnt + ".overrideEnabled", 1)
            cmds.setAttr(jnt + ".overrideColor", col_idx)
        except Exception:
            pass
        try:
            cmds.setAttr(jnt + ".useObjectColor", 1)
            cmds.setAttr(jnt + ".objectColor", b_i % 8)
        except Exception:
            pass

        rx, ry, rz = bone.matrix[0], bone.matrix[1], bone.matrix[2]
        tx, ty, tz = bone.offset[0], bone.offset[1], bone.offset[2]

        m16 = [
            rx[0], rx[1], rx[2], 0.0,
            ry[0], ry[1], ry[2], 0.0,
            rz[0], rz[1], rz[2], 0.0,
            tx,    ty,    tz,    1.0
        ]
        cmds.xform(jnt, worldSpace=True, matrix=m16)
        joint_names.append(jnt)

    cmds.select(clear=True)

    # 3. Group Valid Non-Degenerate Faces by Object
    faces_by_obj = {}
    num_total_verts = len(asset.verts)
    for face in asset.faces:
        if len(face.indices) == 3:
            v0, v1, v2 = face.indices[0], face.indices[1], face.indices[2]
            if (
                0 <= v0 < num_total_verts and
                0 <= v1 < num_total_verts and
                0 <= v2 < num_total_verts and
                v0 != v1 and v1 != v2 and v0 != v2
            ):
                if face.object_id not in faces_by_obj:
                    faces_by_obj[face.object_id] = []
                faces_by_obj[face.object_id].append(face)

    # 4. Build Meshes using Maya 8.5 MFnMesh
    num_joints = len(joint_names)
    for obj_id, obj_faces in faces_by_obj.items():
        if obj_id < len(asset.objects):
            obj_name = sanitize_maya_name(asset.objects[obj_id])
        else:
            obj_name = "%s_mesh_%d" % (model_name, obj_id)

        if cmds.objExists(obj_name):
            obj_name = "%s_%s_%d" % (model_name, obj_name, obj_id)

        global_to_local = {}
        local_verts = []
        local_normals = []
        for f in obj_faces:
            for c_i in range(3):
                g_idx = f.indices[c_i]
                if g_idx not in global_to_local:
                    global_to_local[g_idx] = len(local_verts)
                    local_verts.append(asset.verts[g_idx])
                    if c_i < len(f.normals):
                        local_normals.append(f.normals[c_i])
                    else:
                        local_normals.append((0.0, 0.0, 1.0))

        num_vertices = len(local_verts)
        num_polygons = len(obj_faces)
        if num_vertices == 0 or num_polygons == 0:
            continue

        vertex_array = OpenMaya.MFloatPointArray()
        for v in local_verts:
            vertex_array.append(OpenMaya.MFloatPoint(v.offset[0], v.offset[1], v.offset[2]))

        polygon_counts = OpenMaya.MIntArray()
        polygon_connects = OpenMaya.MIntArray()
        u_array = OpenMaya.MFloatArray()
        v_array = OpenMaya.MFloatArray()
        uv_ids = OpenMaya.MIntArray()

        uv_counter = 0
        for f in obj_faces:
            polygon_counts.append(3)
            order = (0, 2, 1)
            for corner in order:
                g_v = f.indices[corner]
                l_v = global_to_local[g_v]
                polygon_connects.append(l_v)

                if corner < len(f.uvs):
                    uv = f.uvs[corner]
                else:
                    uv = (0.0, 0.0)
                u_array.append(float(uv[0]))
                v_array.append(float(1.0 - uv[1]))
                uv_ids.append(uv_counter)
                uv_counter = uv_counter + 1

        mesh_fn = OpenMaya.MFnMesh()
        new_transform = mesh_fn.create(
            num_vertices,
            num_polygons,
            vertex_array,
            polygon_counts,
            polygon_connects
        )

        dag_fn = OpenMaya.MFnDagNode(new_transform)
        dag_fn.setName(obj_name)
        mesh_full_path = dag_fn.fullPathName()

        try:
            cmds.sets(mesh_full_path, edit=True, forceElement="initialShadingGroup")
        except Exception:
            pass

        try:
            mesh_fn.clearUVs()
            mesh_fn.setUVs(u_array, v_array)
            mesh_fn.assignUVs(polygon_counts, uv_ids)
        except Exception:
            pass

        try:
            norm_array = OpenMaya.MVectorArray()
            vert_idx_array = OpenMaya.MIntArray()
            for l_i in range(num_vertices):
                nx, ny, nz = local_normals[l_i]
                norm_array.append(OpenMaya.MVector(nx, ny, nz))
                vert_idx_array.append(l_i)
            mesh_fn.setVertexNormals(norm_array, vert_idx_array)
        except Exception:
            pass

        mat_faces = {}
        for f_idx in range(len(obj_faces)):
            f = obj_faces[f_idx]
            if f.material_id not in mat_faces:
                mat_faces[f.material_id] = []
            mat_faces[f.material_id].append(f_idx)

        if len(mat_faces) == 1:
            only_mat_id = mat_faces.keys()[0]
            sg = shading_groups.get(only_mat_id, "initialShadingGroup")
            try:
                cmds.sets(mesh_full_path, edit=True, forceElement=sg)
            except Exception:
                pass
        else:
            for mat_id, face_list in mat_faces.items():
                sg = shading_groups.get(mat_id, "initialShadingGroup")
                face_comps = []
                for fi in face_list:
                    face_comps.append("%s.f[%d]" % (mesh_full_path, fi))
                try:
                    cmds.sets(face_comps, edit=True, forceElement=sg)
                except Exception:
                    pass

        if num_joints > 0:
            try:
                skin_args = list(joint_names)
                skin_args.append(mesh_full_path)
                skin_cluster = cmds.skinCluster(
                    *skin_args,
                    **{"toSelectedBones": True, "maximumInfluences": 4}
                )[0]

                cmds.setAttr(skin_cluster + ".normalizeWeights", 0)
                last_j_idx = num_joints - 1

                for l_idx in range(num_vertices):
                    v_data = local_verts[l_idx]
                    if v_data.weights:
                        if num_joints > 1:
                            w_attr = "%s.wl[%d].w[0:%d]" % (skin_cluster, l_idx, last_j_idx)
                            w_list = [0.0] * num_joints
                            for b_idx, w_val in v_data.weights:
                                if 0 <= b_idx < num_joints:
                                    w_list[b_idx] = float(w_val)
                            cmds.setAttr(w_attr, *w_list)
                        else:
                            cmds.setAttr("%s.wl[%d].w[0]" % (skin_cluster, l_idx), float(v_data.weights[0][1]))

                cmds.setAttr(skin_cluster + ".normalizeWeights", 1)
            except Exception:
                pass

    cmds.select(clear=True)
    enable_smooth_shaded_viewports()


# ==============================================================================
# 7. XANIM APPLICATION (Synced World-Space Keyframing)
# ==============================================================================

def find_joint_in_maya(part_name):
    if cmds.objExists(part_name) and cmds.nodeType(part_name) == "joint":
        return part_name

    sanitized = sanitize_maya_name(part_name)
    if cmds.objExists(sanitized) and cmds.nodeType(sanitized) == "joint":
        return sanitized

    clean = part_name.strip("\"' ")
    if cmds.objExists(clean) and cmds.nodeType(clean) == "joint":
        return clean

    all_joints = cmds.ls(type="joint")
    if not all_joints:
        return None

    clean_lower = clean.lower()
    sanitized_lower = sanitized.lower()

    for jnt in all_joints:
        jnt_base = jnt.split("|")[-1].split(":")[-1].lower()
        if jnt_base == clean_lower or jnt_base == sanitized_lower:
            return jnt

    for jnt in all_joints:
        jnt_lower = jnt.lower()
        if (
            jnt_lower.endswith("_" + clean_lower) or
            jnt_lower.endswith(":" + clean_lower) or
            jnt_lower == clean_lower
        ):
            return jnt

    return None


def apply_xanim_to_maya(asset):
    if not asset.frames or not asset.parts:
        print "[BO3 XAnim Importer] Warning: No animation frames found to apply."
        return

    part_map = {}
    for p_idx, p_name in asset.parts:
        jnt = find_joint_in_maya(p_name)
        if jnt:
            part_map[p_idx] = jnt

    if not part_map:
        print "[BO3 XAnim Importer] Error: Could not find matching joints in Maya for animation parts:"
        for p_idx, p_name in asset.parts[:5]:
            print "  - Part %d: '%s'" % (p_idx, p_name)
        return

    fps = asset.framerate
    if fps <= 0.0:
        fps = 30.0

    if abs(fps - 30.0) < 0.5:
        cmds.currentUnit(time="ntsc")
    elif abs(fps - 60.0) < 0.5:
        cmds.currentUnit(time="ntscf")
    elif abs(fps - 24.0) < 0.5:
        cmds.currentUnit(time="film")
    elif abs(fps - 25.0) < 0.5:
        cmds.currentUnit(time="pal")

    min_frame = asset.frames[0].frame_num
    max_frame = asset.frames[-1].frame_num

    cmds.playbackOptions(
        minTime=min_frame,
        maxTime=max_frame,
        animationStartTime=min_frame,
        animationEndTime=max_frame
    )

    total_keys = 0
    # Process frames and synchronize scene time so Maya's DAG evaluates parent matrices correctly
    for frame in asset.frames:
        f_num = frame.frame_num
        cmds.currentTime(f_num, update=True)

        for part in frame.parts:
            jnt = part_map.get(part.part_index)
            if not jnt:
                continue

            rx, ry, rz = part.matrix[0], part.matrix[1], part.matrix[2]
            tx, ty, tz = part.offset[0], part.offset[1], part.offset[2]

            m16 = [
                rx[0], rx[1], rx[2], 0.0,
                ry[0], ry[1], ry[2], 0.0,
                rz[0], rz[1], rz[2], 0.0,
                tx,    ty,    tz,    1.0
            ]

            try:
                # Apply in worldSpace (matching model bind pose)
                cmds.xform(jnt, worldSpace=True, matrix=m16)
                cmds.setKeyframe(
                    jnt,
                    time=f_num,
                    attribute=["translateX", "translateY", "translateZ", "rotateX", "rotateY", "rotateZ"]
                )
                total_keys = total_keys + 1
            except Exception:
                pass

    cmds.currentTime(min_frame, update=True)
    print "[BO3 XAnim Importer] Successfully applied %d frames across %d bones (%d keys set, %g FPS)." % (
        len(asset.frames), len(part_map), total_keys, fps
    )


# ==============================================================================
# 8. TRANSLATOR & REGISTRATION
# ==============================================================================

class BO3XModelTranslator(OpenMayaMPx.MPxFileTranslator):
    def __init__(self):
        OpenMayaMPx.MPxFileTranslator.__init__(self)

    def haveWriteMethod(self):
        return False

    def haveReadMethod(self):
        return True

    def canBeOpened(self):
        return True

    def filter(self):
        return "*.xmodel_bin;*.XMODEL_BIN;*.xmodel_export;*.XMODEL_EXPORT;*.xanim_bin;*.XANIM_BIN;*.xanim_export;*.XANIM_EXPORT"

    def defaultExtension(self):
        return "xmodel_bin"

    def identifyFile(self, file_obj, buffer_str, size):
        fname = str(file_obj.fullName()).lower()
        if (
            fname.endswith(".xmodel_bin") or fname.endswith(".xmodel_export") or
            fname.endswith(".xanim_bin") or fname.endswith(".xanim_export")
        ):
            return OpenMayaMPx.MPxFileTranslator.kIsMyFileType
        return OpenMayaMPx.MPxFileTranslator.kNotMyFileType

    def reader(self, file_obj, option_string, access_mode):
        try:
            filepath = str(file_obj.fullName())
            fl = filepath.lower()

            if fl.endswith(".xanim_bin") or fl.endswith(".xanim_export"):
                print "[BO3 XAnim Importer] Reading: %s" % filepath
                anim_asset = load_xanim_file(filepath)
                apply_xanim_to_maya(anim_asset)
            else:
                print "[BO3 XModel Importer] Reading: %s" % filepath
                asset = load_xmodel_file(filepath)
                model_dir = os.path.dirname(filepath)
                base_name = os.path.splitext(os.path.basename(filepath))[0]
                build_maya_scene(asset, sanitize_maya_name(base_name), model_dir)
                print "[BO3 XModel Importer] Imported: %s (%d bones, %d verts, %d faces)" % (
                    base_name, len(asset.bones), len(asset.verts), len(asset.faces)
                )

            return OpenMaya.MStatus.kSuccess
        except Exception:
            traceback.print_exc()
            raise


def translator_creator():
    return OpenMayaMPx.asMPxPtr(BO3XModelTranslator())


class BO3ImportXModelCmd(OpenMayaMPx.MPxCommand):
    def __init__(self):
        OpenMayaMPx.MPxCommand.__init__(self)

    def doIt(self, args):
        filepath = ""
        if args.length() > 0:
            filepath = args.asString(0)
        else:
            selected = cmds.fileDialog(mode=0, directoryMask="*.xmodel_bin;*.xmodel_export;*.*")
            if selected:
                filepath = str(selected)

        if filepath and os.path.isfile(filepath):
            print "[BO3 XModel Importer] Command importing: %s" % filepath
            asset = load_xmodel_file(filepath)
            model_dir = os.path.dirname(filepath)
            base_name = os.path.splitext(os.path.basename(filepath))[0]
            build_maya_scene(asset, sanitize_maya_name(base_name), model_dir)
            print "[BO3 XModel Importer] Imported: %s (%d bones, %d verts, %d faces)" % (
                base_name, len(asset.bones), len(asset.verts), len(asset.faces)
            )


def model_cmd_creator():
    return OpenMayaMPx.asMPxPtr(BO3ImportXModelCmd())


class BO3ImportXAnimCmd(OpenMayaMPx.MPxCommand):
    def __init__(self):
        OpenMayaMPx.MPxCommand.__init__(self)

    def doIt(self, args):
        filepath = ""
        if args.length() > 0:
            filepath = args.asString(0)
        else:
            selected = cmds.fileDialog(mode=0, directoryMask="*.xanim_bin;*.xanim_export;*.*")
            if selected:
                filepath = str(selected)

        if filepath and os.path.isfile(filepath):
            print "[BO3 XAnim Importer] Command importing: %s" % filepath
            anim_asset = load_xanim_file(filepath)
            apply_xanim_to_maya(anim_asset)


def anim_cmd_creator():
    return OpenMayaMPx.asMPxPtr(BO3ImportXAnimCmd())


def create_bo3_menu():
    try:
        if cmds.about(batch=True):
            return
        g_main_window = mel.eval("$tmpVar=$gMainWindow")
        if not g_main_window:
            return
        if cmds.menu(MENU_NAME, exists=True):
            cmds.deleteUI(MENU_NAME, menu=True)
        cmds.menu(MENU_NAME, label="BO3 Tools", parent=g_main_window, tearOff=True)
        cmds.menuItem(
            label="Import BO3 XModel (.xmodel_bin / .xmodel_export)...",
            command="bo3ImportXModel;"
        )
        cmds.menuItem(
            label="Import BO3 XAnim (.xanim_bin / .xanim_export)...",
            command="bo3ImportXAnim;"
        )
    except Exception:
        pass


def remove_bo3_menu():
    try:
        if cmds.menu(MENU_NAME, exists=True):
            cmds.deleteUI(MENU_NAME, menu=True)
    except Exception:
        pass


def initializePlugin(mobject):
    mplugin = OpenMayaMPx.MFnPlugin(mobject, "BO3 Mod Tools Maya 8.5 Bridge", "3.2", "Any")

    try:
        mplugin.registerFileTranslator(
            TRANSLATOR_NAME,
            None,
            translator_creator
        )
    except Exception:
        pass

    try:
        mplugin.registerCommand(CMD_IMPORT_MODEL, model_cmd_creator)
    except Exception:
        pass

    try:
        mplugin.registerCommand(CMD_IMPORT_ANIM, anim_cmd_creator)
    except Exception:
        pass

    create_bo3_menu()


def uninitializePlugin(mobject):
    mplugin = OpenMayaMPx.MFnPlugin(mobject)
    remove_bo3_menu()
    try:
        mplugin.deregisterFileTranslator(TRANSLATOR_NAME)
    except Exception:
        pass
    try:
        mplugin.deregisterCommand(CMD_IMPORT_MODEL)
    except Exception:
        pass
    try:
        mplugin.deregisterCommand(CMD_IMPORT_ANIM)
    except Exception:
        pass