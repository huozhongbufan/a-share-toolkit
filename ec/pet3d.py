"""瑶瑶 3D 桌宠 —— 用官方 PMX 建模 + PySide6/OpenGL 渲染。

运行要求：素材链 venv 的 Python（装了 PySide6 + numpy）：
  <path-to-python-with-PySide6>
项目自己的零依赖 python 跑不了（没有 PySide6）。

为什么是这条路线：
  · 官方 PMX 是 3D 模型，tkinter 画不了 3D
  · Qt 的窗口支持真·逐像素透明（tkinter 只能洋红抠色，边缘有锯齿）
  · OpenGL 走 RTX 4070 GPU，实时旋转 60fps

蒙皮说明（诚实）：
  · 这个导出器的 PMX 骨骼段与标准不兼容（flags 1字节、尾段布局未知）
  · 当前用**恒等蒙皮**：顶点自带绑定姿势，直接渲染
  · 瑶瑶会以模型自带姿势站着（A-pose 附近），骨骼动画（眨眼/挥手）待骨骼段攻克后加
"""
from __future__ import annotations

import ctypes
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
    QOpenGLTexture, QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from ec.config import load_config
from ec.pmx import load as pmx_load

PMX_PATH = ROOT / "assets" / "yaoyao-pmx" / "瑶瑶皮肤.pmx"

VERT = """
#version 330 core
uniform mat4 u_mvp;
uniform mat4 u_model;
in vec3 a_pos;
in vec3 a_norm;
in vec2 a_uv;
out vec3 v_norm;
out vec2 v_uv;
void main() {
    gl_Position = u_mvp * vec4(a_pos, 1.0);
    v_norm = mat3(u_model) * a_norm;
    v_uv = UV_EXPR;
}
"""

FRAG = """
#version 330 core
uniform sampler2D u_tex;
uniform vec3 u_light;
uniform vec4 u_diffuse;
in vec3 v_norm;
in vec2 v_uv;
out vec4 frag;
void main() {
    vec4 tex = texture(u_tex, v_uv);
    if (tex.a < 0.05) discard;
    vec3 N = normalize(v_norm);
    vec3 L = normalize(u_light);
    float ndl = max(dot(N, L), 0.0);
    float lvl = smoothstep(0.0, 0.18, ndl);   // 两段式 toon
    // 阴影只压到 0.72，尽量保住贴图本色（太暗会认不出颜色）
    float shade = 0.72 + 0.28 * lvl;
    // 材质 diffuse 参与（含 alpha）：脸部五官/白目/口舌都是半透明叠加层
    vec3 col = tex.rgb * u_diffuse.rgb * shade;
    frag = vec4(col, tex.a * u_diffuse.a);
}
"""


def mat4_perspective(fovy_deg, aspect, znear, zfar):
    f = 1.0 / math.tan(math.radians(fovy_deg) / 2.0)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (zfar + znear) / (znear - zfar)
    m[2, 3] = (2 * zfar * znear) / (znear - zfar)
    m[3, 2] = -1.0
    return m


def mat4_lookat(eye, center, up):
    f = np.array(center, np.float32) - np.array(eye, np.float32)
    f = f / np.linalg.norm(f)
    s = np.cross(f, np.array(up, np.float32))
    s = s / np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3] = s
    m[1, :3] = u
    m[2, :3] = -f
    m[0, 3] = -np.dot(s, eye)
    m[1, 3] = -np.dot(u, eye)
    m[2, 3] = np.dot(f, eye)
    return m


def mat4_roty(rad):
    c, s = math.cos(rad), math.sin(rad)
    m = np.eye(4, dtype=np.float32)
    m[0, 0], m[0, 2] = c, s
    m[2, 0], m[2, 2] = -s, c
    return m


class GLView(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle = 0.0
        self.prog = None
        self.vao = None
        self.vbo = None
        self.ibo = None
        self.textures = {}
        self.ranges = []       # [(start_tri, count_tri, tex_idx, no_cull)]
        self.center = np.zeros(3, np.float32)
        self.scale = 1.0
        self.ok = False
        self.error = ""
        self._load()

    # ---------------------------------------------------------- 数据

    def _load(self):
        try:
            pmx = pmx_load(str(PMX_PATH))
        except Exception as exc:
            self.error = f"模型加载失败：{exc}"
            return

        verts = pmx.vertices
        idx = pmx.indices
        pos = np.array([v.pos for v in verts], np.float32)
        nrm = np.array([v.normal for v in verts], np.float32)
        uvv = np.array([v.uv for v in verts], np.float32)

        lo = pos.min(axis=0)
        hi = pos.max(axis=0)
        self.center = (lo + hi) / 2
        span = (hi - lo).max()
        self.scale = 9.0 / span if span > 0 else 1.0

        interleaved = np.hstack([pos, nrm, uvv]).astype(np.float32)
        # 展开索引：glDrawElements 在这个 PySide6 组合下报 INVALID_OPERATION
        # （offset 参数封装问题），改用 glDrawArrays —— 非索引渲染，
        # 447795 顶点 × 32B = 14MB，量级完全可控。
        self.pos_attr = interleaved[np.array(idx, np.int64)]
        self.total_verts = len(idx)
        self.index_data = None

        base = 0
        self.ranges = []
        for m in pmx.materials:
            count = m.face_count          # 三角形数
            tex_idx = m.tex_idx if 0 <= m.tex_idx < len(pmx.textures) else -1
            no_cull = bool(m.flags & 0x01)
            self.ranges.append((base, count, tex_idx, no_cull, m.name_jp,
                                tuple(m.diffuse)))
            base += count

        self.tex_files = [str(ROOT / "assets" / "yaoyao-pmx" / t)
                          for t in pmx.textures]
        self.ok = True

    # ---------------------------------------------------------- GL

    def initializeGL(self):
        if not self.ok:
            return
        try:
            self.prog = QOpenGLShaderProgram()
            _vert = VERT.replace(
                "UV_EXPR",
                getattr(self, "uv_expr", "vec2(a_uv.x, a_uv.y)"))
            if not self.prog.addShaderFromSourceCode(QOpenGLShader.Vertex, _vert):
                self.error = f"vertex shader: {self.prog.log()}"
                return
            if not self.prog.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAG):
                self.error = f"fragment shader: {self.prog.log()}"
                return
            self.prog.link()
            if not self.prog.isLinked():
                self.error = f"link: {self.prog.log()}"
                return

            self.vao = QOpenGLVertexArrayObject()
            self.vao.create()
            self.vao.bind()
            self.vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            self.vbo.create()
            self.vbo.bind()
            self.vbo.allocate(self.pos_attr.tobytes(),
                              self.pos_attr.nbytes)
            stride = 8 * 4
            # 属性位置必须查询，不能假定是 0/1/2
            loc_pos = self.prog.attributeLocation("a_pos")
            loc_nrm = self.prog.attributeLocation("a_norm")
            loc_uv = self.prog.attributeLocation("a_uv")
            self._attr_locs = (loc_pos, loc_nrm, loc_uv)
            # 编译器可能优化掉未使用的属性（loc=-1），跳过即可，不算错误
            for loc, off, n in ((loc_pos, 0, 3), (loc_nrm, 12, 3), (loc_uv, 24, 2)):
                if loc >= 0:
                    self.prog.enableAttributeArray(loc)
                    self.prog.setAttributeBuffer(loc, 0x1406, off, n, stride)
            if loc_pos < 0 or loc_uv < 0:
                self.error = (f"关键属性缺失: pos={loc_pos} uv={loc_uv}")
                self.ok = False
            self.vao.release()

            for i, fp in enumerate(self.tex_files):
                img = QtGui.QImage(fp)
                if img.isNull():
                    self.textures[i] = None
                    continue
                tex = QOpenGLTexture(QOpenGLTexture.Target2D)
                tex.create()
                # 手动上传：不用 Qt 的 setData(QImage) 便捷接口 ——
                # 实测它上传后的采样结果和 CPU 侧 QImage.pixelColor 不一致
                #（毛发材质 CPU 采到棕色 167,138,123，GPU 渲染却是奶油白）。
                # 显式指定 RGBA8 + 原始字节，行序 = 图像自上而下（t=0 在顶部），
                # 与 PMX 的 UV 约定一致。
                img2 = img.convertToFormat(QtGui.QImage.Format_RGBA8888)
                tw, th = img2.width(), img2.height()
                raw = np.frombuffer(img2.constBits(), np.uint8,
                                    count=img2.bytesPerLine() * th).copy()
                raw = raw.reshape(th, img2.bytesPerLine() // 4, 4)[:, :tw, :]
                tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
                tex.setSize(tw, th)
                tex.setMipLevels(1)
                tex.allocateStorage()
                tex.setData(QOpenGLTexture.PixelFormat.RGBA,
                            QOpenGLTexture.PixelType.UInt8,
                            np.ascontiguousarray(raw).tobytes())
                tex.setMinificationFilter(QOpenGLTexture.Linear)
                tex.setMagnificationFilter(QOpenGLTexture.Linear)
                tex.setWrapMode(QOpenGLTexture.ClampToEdge)
                self.textures[i] = tex
            self.ok = True
        except Exception as exc:
            self.error = f"GL 初始化失败：{exc}"
            self.ok = False

    def paintGL(self):
        gl = self.context().functions()
        errs = []
        def chk(tag):
            e = gl.glGetError()
            errs.append(f"{tag}={e}")
        chk("start")
        gl.glClearColor(0, 0, 0, 0)
        gl.glClear(0x4000 | 0x100)      # color | depth
        chk("clear")
        gl.glEnable(0x0B71)             # GL_DEPTH_TEST
        gl.glDisable(0x0C11)
        chk("state")
        if not self.ok or self.prog is None:
            self.err_seq = errs
            return

        aspect = self.width() / max(1, self.height())
        proj = mat4_perspective(30, aspect, 0.1, 100)
        # 相机：看向模型的中心（缩放后），距离按模型高度取
        # 模型已平移到「包围盒中心 = 原点」，相机就看原点，
        # 不能再叠一层 center*scale —— 之前两套偏移叠加导致她沉到画面外。
        dist = 24.0
        eye = (0.0, 0.0, dist)
        view = mat4_lookat(eye, (0, 0, 0), (0, 1, 0))
        # 模型正面朝 -Z，相机在 +Z：基础转 180° 让她面向观众
        model = (mat4_roty(self.angle + math.pi)
                 @ np.array([[self.scale, 0, 0, 0],
                             [0, self.scale, 0, 0],
                             [0, 0, self.scale, 0],
                             [0, 0, 0, 1]], np.float32))
        model[0, 3] = -self.center[0] * self.scale
        model[1, 3] = -self.center[1] * self.scale
        model[2, 3] = -self.center[2] * self.scale
        mvp = proj @ view @ model

        self.prog.bind()
        chk("bind_prog")
        self.prog.setUniformValue("u_mvp",
                                  QtGui.QMatrix4x4(*mvp.flatten()))
        self.prog.setUniformValue("u_model",
                                  QtGui.QMatrix4x4(*model.flatten()))
        self.prog.setUniformValue("u_light", QtGui.QVector3D(0.4, 0.8, 0.6))
        chk("uniforms")
        self.vao.bind()
        chk("bind_vao")

        # alpha 混合：脸的五官/白目/口舌是半透明叠加层，不混合就是苍白面具
        gl.glEnable(0x0BE2)                      # GL_BLEND
        gl.glBlendFunc(0x0302, 0x0303)           # SRC_ALPHA, ONE_MINUS_SRC_ALPHA
        gl.glDepthMask(1)

        # 先画不透明（alpha≈1），再画半透明层，避免叠加层被深度挡住
        order = sorted(range(len(self.ranges)),
                       key=lambda i: self.ranges[i][5][3] < 0.99)
        drawn = 0
        for n, i in enumerate(order):
            start, count, tex_idx, no_cull, _name, diff = self.ranges[i]
            if no_cull:
                gl.glDisable(0x0C11)
            else:
                gl.glEnable(0x0C11)
            tex = self.textures.get(tex_idx)
            if tex is not None:
                tex.bind()
            self.prog.setUniformValue("u_tex", 0)
            self.prog.setUniformValue("u_diffuse",
                                      QtGui.QVector4D(*diff))
            gl.glDrawArrays(0x0004, start * 3, count * 3)
            if n == 0:
                chk("draw0")
            drawn += count * 3
            if tex is not None:
                tex.release()
        chk("draw_all")
        self.last_gl_error = gl.glGetError()
        self.last_drawn = drawn
        self.err_seq = errs
        self.vao.release()
        self.prog.release()

    def step(self):
        # 小幅摆身（±25°），始终面向观众 —— 桌宠不该一直转圈
        import math as _m
        self._t = getattr(self, "_t", 0.0) + 0.02
        self.angle = _m.sin(self._t) * 0.44
        self.update()


class ModelLabel(QtWidgets.QLabel):
    """把 GL 帧缓冲拷成图，用普通 QPainter 画出来。

    为什么：PySide6 在 Windows 上 QOpenGLWidget + 透明窗口的组合，
    帧缓冲渲染正常（grabFramebuffer 有内容）但**不合成到屏幕**——
    实测整窗透明、什么都看不见。绕法：每帧 grabFramebuffer →
    QImage → QLabel 画（CPU 合成，透明像素保持透明）。
    """

    def __init__(self, gl_view, parent=None):
        super().__init__(parent)
        self.gl_view = gl_view
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)

    def refresh(self):
        try:
            fb = self.gl_view.grabFramebuffer()
            pm = QtGui.QPixmap.fromImage(fb)
            # 帧缓冲是设备像素（DPR 1.5），必须标 DPR 让 Qt 缩回逻辑尺寸，
            # 否则 480x701 的图塞进 320x467 的标签只显示左上角一块
            pm.setDevicePixelRatio(self.devicePixelRatioF())
            self.setPixmap(pm)
        except Exception:
            pass


class PetWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint
                            | QtCore.Qt.WindowStaysOnTopHint
                            | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.resize(320, 500)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.bubble = QtWidgets.QLabel("")
        self.bubble.setStyleSheet(
            "background:#ffffff; color:#1f2430; border-radius:10px;"
            "padding:8px 12px; font-size:13px;")
        self.bubble.setWordWrap(True)
        self.bubble.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.bubble.setMaximumWidth(300)
        self.bubble.hide()
        lay.addWidget(self.bubble)

        # 载入层：GL 部件负责渲染，ModelLabel 负责把帧缓冲画到屏幕上。
        # 关键：Qt 里 QOpenGLWidget 的子部件不会画在它上面（已知限制），
        # 所以 ModelLabel 必须是 GL 的**兄弟**，再 raise 到上层。
        self.stage = QtWidgets.QWidget(self)
        lay.addWidget(self.stage, 1)
        self.gl = GLView(self.stage)
        if self.gl.error:
            self.bubble.setText(f"瑶瑶：{self.gl.error}")
            self.bubble.show()
        self.model = ModelLabel(self.gl, self.stage)
        self.model.raise_()

        def _relayout(_e=None):
            r = self.stage.rect()
            self.gl.setGeometry(r)
            self.model.setGeometry(r)
            self.model.raise_()
        self.stage.resizeEvent = _relayout

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._frame)
        self.timer.start(33)

        self._drag = None
        self._say("瑶瑶在～（官方模型 · 恒等蒙皮预览）")

    def _frame(self):
        self.gl.step()
        self.model.refresh()

    # ---------------------------------------------------------- 交互

    def _say(self, text, ms=6000):
        self.bubble.setText(text)
        self.bubble.show()
        QtCore.QTimer.singleShot(ms, self._fade)

    def _fade(self):
        self.bubble.hide()

    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == QtCore.Qt.RightButton:
            menu = QtWidgets.QMenu(self)
            act_quit = menu.addAction("退出")
            menu.addSeparator()
            menu.addAction(f"旋转中 · 拖动可移动")
            chosen = menu.exec(e.globalPosition().toPoint())
            if chosen == act_quit:
                QtWidgets.QApplication.quit()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self._say("双击瑶瑶可以问行情——还没接，先看 3D 吧")


def run(cfg=None) -> None:
    fmt = QtGui.QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(0)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)

    app = QtWidgets.QApplication(sys.argv)
    w = PetWindow()
    # 放屏幕内（逻辑分辨率 1707x960）：右下角
    scr = app.primaryScreen().availableGeometry()
    w.move(scr.right() - w.width() - 40, scr.bottom() - w.height() - 60)
    w.show()

    def _snap():
        try:
            pm = w.model.pixmap()
            if pm:
                pm.save(str(ROOT / "out" / "_snap.png"))
        except Exception:
            pass
    QtCore.QTimer.singleShot(4000, _snap)
    sys.exit(app.exec())


if __name__ == "__main__":
    run(load_config())
