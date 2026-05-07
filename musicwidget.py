#!/usr/bin/env python3
"""
Aurora Music Widget — Linux
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Resizable floating card · animated accent glow · right-click settings
MPRIS2 via DBus · Wayland + X11

Resize: drag any edge or corner. Minimum 220×300.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations
import sys, os, io, json, time, threading, urllib.request, math
from pathlib import Path
from typing  import Optional

# Linux only

# ── PyQt6 ─────────────────────────────────────────────────────────────────────
try:
    from PyQt6.QtWidgets import (
        QApplication, QWidget, QSizePolicy, QLabel, QSlider,
        QVBoxLayout, QHBoxLayout, QCheckBox, QPushButton, QFrame,
    )
    from PyQt6.QtCore import (
        Qt, QTimer, QThread, pyqtSignal, QPointF, QRectF, QPoint, QSize, QRect,
    )
    from PyQt6.QtGui import (
        QPainter, QColor, QLinearGradient, QRadialGradient, QPainterPath,
        QBrush, QPen, QPixmap, QFont, QFontMetrics, QCursor,
    )
except ImportError:
    sys.exit("PyQt6 required — pip install PyQt6")

# ── Optional colour extraction ────────────────────────────────────────────────
try:
    from colorthief import ColorThief
    COLOR_OK = True
except ImportError:
    COLOR_OK = False

# ── DBus / MPRIS2 ─────────────────────────────────────────────────────────────
DBUS_OK = False
try:
    import dbus
    DBUS_OK = True
except ImportError:
    print("[aurora] dbus-python not found — demo mode")

CFG_PATH = Path.home() / ".config" / "aurora-widget" / "settings.json"

# ── Design constants (base card size; actual dims come from window size) ──────
GLOW_PAD    = 36          # transparent border for glow bleed
MIN_W       = 220         # minimum card width
MIN_H       = 300         # minimum card height
DEFAULT_W   = 300
DEFAULT_H   = 430
EDGE_HIT    = 10          # px from edge counted as resize zone
CORNER_HIT  = 18          # px from corner counted as corner resize zone
FPS         = 60

# ── Default palette ───────────────────────────────────────────────────────────
C_BG   = QColor(18,  16,  24,  225)
C_ACC  = QColor(140, 100, 255)
C_FG   = QColor(240, 235, 255)
C_MUT  = QColor(140, 128, 160)
C_PROG = QColor(30,  27,  40,  175)

# ── Default settings ──────────────────────────────────────────────────────────
DEFAULTS: dict = {
    "opacity":        0.95,
    "glow_intensity": 0.75,
    "glow_size":      0.65,
    "always_on_top":  False,
    "show_progress":  True,
    "show_time":      True,
    "corner_radius":  22,
    "bg_alpha":       225,
    "win_w":          DEFAULT_W,
    "win_h":          DEFAULT_H,
    "win_x":          -1,
    "win_y":          -1,
}


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════
def lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red()+(b.red()-a.red())*t),
                  int(a.green()+(b.green()-a.green())*t),
                  int(a.blue()+(b.blue()-a.blue())*t),
                  int(a.alpha()+(b.alpha()-a.alpha())*t))

def luminance(c: QColor) -> float:
    def g(v): return v/12.92 if v <= 0.04045 else ((v+0.055)/1.055)**2.4
    return 0.2126*g(c.redF()) + 0.7152*g(c.greenF()) + 0.0722*g(c.blueF())

def readable(fg: QColor, bg: QColor, ratio: float = 4.5) -> QColor:
    for _ in range(60):
        lf = luminance(fg)+0.05; lb = luminance(bg)+0.05
        if max(lf,lb)/min(lf,lb) >= ratio: break
        h,s,v,a = fg.getHsvF(); fg = QColor.fromHsvF(h, s, min(1.0, v+0.03), a)
    return fg

def load_cfg() -> dict:
    try:
        if CFG_PATH.exists():
            d = json.loads(CFG_PATH.read_text())
            s = dict(DEFAULTS); s.update(d); return s
    except: pass
    return dict(DEFAULTS)

def save_cfg(s: dict):
    try: CFG_PATH.parent.mkdir(parents=True, exist_ok=True); CFG_PATH.write_text(json.dumps(s, indent=2))
    except: pass


# ══════════════════════════════════════════════════════════════════════════════
#  Animators
# ══════════════════════════════════════════════════════════════════════════════
class Anim:
    def __init__(self, v=0.0, spd=0.10): self.cur=v; self.tgt=v; self.spd=spd
    def tick(self) -> bool:
        d = self.tgt - self.cur
        if abs(d) < 0.0007: self.cur=self.tgt; return False
        self.cur += d*self.spd; return True

class AnimC:
    def __init__(self, c: QColor, spd=0.07): self.cur=c; self.tgt=c; self.spd=spd
    def tick(self) -> bool:
        dr=self.tgt.red()-self.cur.red();   dg=self.tgt.green()-self.cur.green()
        db=self.tgt.blue()-self.cur.blue(); da=self.tgt.alpha()-self.cur.alpha()
        if abs(dr)+abs(dg)+abs(db)+abs(da) < 1.1: self.cur=self.tgt; return False
        self.cur = QColor(int(self.cur.red()+dr*self.spd), int(self.cur.green()+dg*self.spd),
                          int(self.cur.blue()+db*self.spd), int(self.cur.alpha()+da*self.spd))
        return True


# ══════════════════════════════════════════════════════════════════════════════
#  Track model
# ══════════════════════════════════════════════════════════════════════════════
class Track:
    __slots__ = ("title","artist","album","art_url","art_bytes",
                 "duration","position","playing","player")
    def __init__(self):
        self.title="No media playing"; self.artist=""; self.album=""
        self.art_url=""; self.art_bytes=None
        self.duration=0; self.position=0; self.playing=False; self.player=""
    def progress(self) -> float:
        return max(0.0, min(1.0, self.position/self.duration)) if self.duration > 0 else 0.0
    def fmt(self, us: int) -> str:
        s = max(0,us)//1_000_000; return f"{s//60}:{s%60:02d}"


# ══════════════════════════════════════════════════════════════════════════════
#  Media backends
# ══════════════════════════════════════════════════════════════════════════════

class DBusBackend:
    """Linux + FreeBSD — MPRIS2 over DBus."""
    def __init__(self):
        self._bus=None; self._art_url=""; self._art_bytes=None; self._last=""
        if DBUS_OK:
            try: self._bus=dbus.SessionBus()
            except Exception as e: print(f"[dbus] {e}")

    def poll(self) -> Track:
        t = Track()
        if not self._bus: return t
        try:
            di = dbus.Interface(
                self._bus.get_object("org.freedesktop.DBus","/org/freedesktop/DBus"),
                "org.freedesktop.DBus")
            players = [n for n in di.ListNames() if n.startswith("org.mpris.MediaPlayer2.")]
            if not players: return t
            chosen = self._last if self._last in players else players[0]; self._last=chosen
            obj = self._bus.get_object(chosen, "/org/mpris/MediaPlayer2")
            pr  = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            meta= pr.Get("org.mpris.MediaPlayer2.Player","Metadata")
            stat= str(pr.Get("org.mpris.MediaPlayer2.Player","PlaybackStatus"))
            t.player=chosen; t.playing=(stat=="Playing")
            t.title = str(meta.get("xesam:title","Unknown")) or "Unknown"
            t.artist= ", ".join(str(a) for a in meta.get("xesam:artist",[])) or ""
            t.album = str(meta.get("xesam:album","")) or ""
            t.duration=int(meta.get("mpris:length",0))
            t.art_url=str(meta.get("mpris:artUrl",""))
            try: t.position=int(pr.Get("org.mpris.MediaPlayer2.Player","Position"))
            except: pass
            if t.art_url and t.art_url!=self._art_url:
                self._art_bytes=self._fetch(t.art_url); self._art_url=t.art_url
            t.art_bytes=self._art_bytes
        except Exception as e: print(f"[mpris] {e}")
        return t

    def cmd(self, player: str, c: str):
        if not player: return
        try:
            obj=dbus.SessionBus().get_object(player,"/org/mpris/MediaPlayer2")
            getattr(dbus.Interface(obj,"org.mpris.MediaPlayer2.Player"),c)()
        except: pass

    def seek(self, player: str, frac: float):
        if not player: return
        try:
            bus=dbus.SessionBus(); obj=bus.get_object(player,"/org/mpris/MediaPlayer2")
            pr=dbus.Interface(obj,"org.freedesktop.DBus.Properties")
            meta=pr.Get("org.mpris.MediaPlayer2.Player","Metadata")
            dur=int(meta.get("mpris:length",0))
            tid=meta.get("mpris:trackid","/org/mpris/MediaPlayer2/TrackList/NoTrack")
            dbus.Interface(obj,"org.mpris.MediaPlayer2.Player").SetPosition(tid,dbus.Int64(int(dur*frac)))
        except: pass

    @staticmethod
    def _fetch(url: str) -> Optional[bytes]:
        try:
            if url.startswith("file://"): return open(url[7:],"rb").read()
            return urllib.request.urlopen(
                urllib.request.Request(url,headers={"User-Agent":"aurora/4"}),timeout=5).read()
        except: return None




class DemoBackend:
    _TRACKS=[("Midnight City","M83","Hurry Up, We're Dreaming",237_000_000),
             ("Electric Feel","MGMT","Oracular Spectacular",225_000_000),
             ("Redbone","Childish Gambino","Awaken, My Love!",327_000_000),
             ("Nights","Frank Ocean","Blonde",348_000_000)]
    def poll(self) -> Track:
        i=(int(time.time())//28)%len(self._TRACKS); t=Track()
        t.title,t.artist,t.album,t.duration=self._TRACKS[i]
        t.position=int((time.time()%28)/28*t.duration); t.playing=True; t.player="demo"; return t
    def cmd(self,*_): pass
    def seek(self,*_): pass


def _make_backend():
    return DBusBackend() if DBUS_OK else DemoBackend()


# ══════════════════════════════════════════════════════════════════════════════
#  Worker thread
# ══════════════════════════════════════════════════════════════════════════════
class MediaWorker(QThread):
    updated = pyqtSignal(object)
    def __init__(self):
        super().__init__(); self._run=True; self._be=_make_backend()
    def stop(self): self._run=False
    def cmd(self,p,c): threading.Thread(target=self._be.cmd,args=(p,c),daemon=True).start()
    def seek(self,p,f): threading.Thread(target=self._be.seek,args=(p,f),daemon=True).start()
    def run(self):
        while self._run:
            try: self.updated.emit(self._be.poll())
            except Exception as e: print(f"[worker] {e}")
            self.msleep(500)


# ══════════════════════════════════════════════════════════════════════════════
#  Palette extraction
# ══════════════════════════════════════════════════════════════════════════════
def extract_palette(img_bytes: bytes) -> Optional[dict]:
    if not COLOR_OK or not img_bytes: return None
    try:
        ct=ColorThief(io.BytesIO(img_bytes)); dom=ct.get_color(quality=1)
        palette=ct.get_palette(color_count=4, quality=1)
        acc=QColor(*dom); h,s,v,_=acc.getHsvF()
        acc   = QColor.fromHsvF(h, min(1.0,s*1.45), min(1.0,v*1.1))
        # Background: very dark base, tinted with album hue
        bg    = QColor.fromHsvF(h, 0.35, 0.06, 0.95)
        # Mid gradient stop: slightly less dark, same hue
        bg_mid= QColor.fromHsvF(h, 0.28, 0.10, 0.95)
        # Gradient bottom: accent-tinted, darker
        h2    = (h + 0.05) % 1.0
        bg_bot= QColor.fromHsvF(h2, 0.40, 0.08, 0.95)
        fg    = readable(QColor.fromHsvF(h, 0.05, 0.97), bg)
        return dict(acc=acc, bg=bg, bg_mid=bg_mid, bg_bot=bg_bot, fg=fg,
                    muted  = QColor.fromHsvF(h, 0.18, 0.62),
                    prog_bg= QColor.fromHsvF(h, 0.22, 0.14, 0.82),
                    glow   = QColor.fromHsvF(h, min(1.0,s*1.2), min(1.0,v)))
    except Exception as e:
        print(f"[palette] {e}"); return None


# ══════════════════════════════════════════════════════════════════════════════
#  Icons — real SVG paths from Material Design Icons (24×24 grid, scaled to sz)
#  Each function receives painter, centre (cx,cy), button pixel size, and colour.
# ══════════════════════════════════════════════════════════════════════════════

def _svg_path(p: QPainter, cx: float, cy: float, sz: float,
              col: QColor, d_commands: list):
    """
    Render a list of (cmd, *coords) path commands drawn on a 24×24 SVG grid,
    centred and scaled so the 24×24 box fits inside `sz` pixels.
    cmd: 'M'=moveTo  'L'=lineTo  'C'=cubicTo  'Z'=close
    coords are in SVG 24×24 space.
    """
    scale  = sz / 24.0
    ox     = cx - 12 * scale    # top-left origin of the 24×24 box in widget coords
    oy     = cy - 12 * scale

    def tx(x): return ox + x * scale
    def ty(y): return oy + y * scale

    path = QPainterPath()
    for cmd in d_commands:
        op = cmd[0]
        if op == 'M':
            path.moveTo(tx(cmd[1]), ty(cmd[2]))
        elif op == 'L':
            path.lineTo(tx(cmd[1]), ty(cmd[2]))
        elif op == 'C':
            path.cubicTo(tx(cmd[1]), ty(cmd[2]),
                         tx(cmd[3]), ty(cmd[4]),
                         tx(cmd[5]), ty(cmd[6]))
        elif op == 'Z':
            path.closeSubpath()

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(col))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(path)
    p.restore()


# Material Design: play_arrow  (filled triangle, visually centred on 24×24)
_PLAY_PATH = [
    ('M', 8, 5), ('L', 19, 12), ('L', 8, 19), ('Z'),
]

# Material Design: pause  (two rounded bars)
def _PAUSE_BARS(sz):
    """Return rounded-rect pairs for pause icon, scaled to sz."""
    s = sz / 24.0
    bw = 3.2 * s; bh = 11 * s; rr = 1.4 * s
    lx = sz/2 - 4.2*s; rx = sz/2 + 0.6*s; ty2 = sz/2 - bh
    return [(lx, ty2, bw, bh*2, rr), (rx, ty2, bw, bh*2, rr)]

# Material Design: skip_previous
_PREV_PATH = [
    # back bar (filled rect via two triangles → use rect drawn separately)
    # triangle pointing left
    ('M', 7, 6), ('L', 7, 18), ('Z'),        # placeholder — we draw bar+tri below
]

# Material Design: skip_next — mirror of prev


def _play(p: QPainter, cx: float, cy: float, sz: float, col: QColor):
    """Material play_arrow icon."""
    scale = sz / 24.0
    ox = cx - 12 * scale; oy = cy - 12 * scale
    path = QPainterPath()
    path.moveTo(ox + 8*scale,  oy + 5*scale)
    path.lineTo(ox + 19*scale, oy + 12*scale)
    path.lineTo(ox + 8*scale,  oy + 19*scale)
    path.closeSubpath()
    p.save(); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(col)); p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(path); p.restore()


def _pause(p: QPainter, cx: float, cy: float, sz: float, col: QColor):
    """Material pause icon — two plain rectangular bars, no rounding."""
    p.save(); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(col)); p.setPen(Qt.PenStyle.NoPen)
    s = sz / 24.0
    bw = 3.5 * s; bh = 10.5 * s
    lx = cx - 6.0*s; rx = cx + 2.5*s
    ty2 = cy - bh
    p.drawRect(QRectF(lx, ty2, bw, bh * 2))
    p.drawRect(QRectF(rx, ty2, bw, bh * 2))
    p.restore()


def _prev(p: QPainter, cx: float, cy: float, sz: float, col: QColor):
    """Material skip_previous — vertical bar + left-pointing triangle."""
    p.save(); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(col)); p.setPen(Qt.PenStyle.NoPen)
    s = sz / 24.0
    # bar: x=6 on 24-grid, width 2, height 12, centred vertically
    bx = cx - 7.5*s; bw = 2.5*s; bh = 6.0*s; rr = bw * 0.45
    p.drawRoundedRect(QRectF(bx, cy - bh, bw, bh * 2), rr, rr)
    # triangle tip at x=9, flat side at x=18, centred vertically
    tri = QPainterPath()
    tri.moveTo(bx + bw + 1.5*s, cy)           # tip (left)
    tri.lineTo(bx + bw + 1.5*s + 9*s, cy - 6*s)   # top-right
    tri.lineTo(bx + bw + 1.5*s + 9*s, cy + 6*s)   # bottom-right
    tri.closeSubpath()
    p.drawPath(tri); p.restore()


def _next(p: QPainter, cx: float, cy: float, sz: float, col: QColor):
    """Material skip_next — right-pointing triangle + vertical bar."""
    p.save(); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(col)); p.setPen(Qt.PenStyle.NoPen)
    s = sz / 24.0
    # bar at right
    bx = cx + 5.0*s; bw = 2.5*s; bh = 6.0*s; rr = bw * 0.45
    p.drawRoundedRect(QRectF(bx, cy - bh, bw, bh * 2), rr, rr)
    # triangle pointing right, flat side to left of bar
    tri = QPainterPath()
    tri.moveTo(bx - 1.5*s,        cy)           # tip (right)
    tri.lineTo(bx - 1.5*s - 9*s,  cy - 6*s)    # top-left
    tri.lineTo(bx - 1.5*s - 9*s,  cy + 6*s)    # bottom-left
    tri.closeSubpath()
    p.drawPath(tri); p.restore()


# ══════════════════════════════════════════════════════════════════════════════
#  Icon button  (size tracks parent resize via explicit setFixedSize calls)
# ══════════════════════════════════════════════════════════════════════════════
class IconBtn(QWidget):
    clicked = pyqtSignal()
    def __init__(self, draw_fn, size=44, primary=False, parent=None):
        super().__init__(parent); self.draw=draw_fn; self._pri=primary
        self._hov=Anim(0,.20); self._prs=Anim(0,.35)
        self._acc=AnimC(C_ACC); self._fg=AnimC(C_MUT)
        self.setFixedSize(size,size); self.setCursor(Qt.CursorShape.PointingHandCursor)
    def set_acc(self,c): self._acc.tgt=c
    def set_fg(self,c):  self._fg.tgt=c
    def resize_to(self, sz: int): self.setFixedSize(sz,sz)
    def tick(self):
        if any([self._hov.tick(),self._prs.tick(),self._acc.tick(),self._fg.tick()]): self.update()
    def enterEvent(self,_): self._hov.tgt=1.0
    def leaveEvent(self,_): self._hov.tgt=0.0
    def mousePressEvent(self,_): self._prs.tgt=1.0
    def mouseReleaseEvent(self,e):
        self._prs.tgt=0.0
        if e.button()==Qt.MouseButton.LeftButton: self.clicked.emit()
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s=self.width(); cx=s/2; cy=s/2; h=self._hov.cur; pr=self._prs.cur; sc=1-pr*.07
        # No background circle — icon floats directly. Subtle hover glow only.
        if h > 0.01:
            glow = QRadialGradient(cx, cy, s/2)
            gc = QColor(self._acc.cur); gc.setAlpha(int(35 * h))
            glow.setColorAt(0, gc); gc2 = QColor(gc); gc2.setAlpha(0); glow.setColorAt(1, gc2)
            p.fillRect(0, 0, s, s, QBrush(glow))
        if self._pri:
            icon_c = QColor(self._acc.cur)
            icon_c.setAlpha(int(200 + 55 * h - pr * 40))
        else:
            icon_c = QColor(self._fg.cur); icon_c.setAlpha(int(150 + 105 * h))
        p.save(); p.translate(cx,cy); p.scale(sc,sc); p.translate(-cx,-cy)
        self.draw(p,cx,cy,s,icon_c); p.restore(); p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  Progress bar
# ══════════════════════════════════════════════════════════════════════════════
class ProgBar(QWidget):
    seeked = pyqtSignal(float)
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedHeight(18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prog=Anim(0,.06); self._hov=Anim(0,.20)
        self._acc=AnimC(C_ACC); self._bg=AnimC(C_PROG)
    def set_prog(self,v): self._prog.tgt=max(0.0,min(1.0,v))
    def set_acc(self,c):  self._acc.tgt=c
    def tick(self):
        if any([self._prog.tick(),self._hov.tick(),self._acc.tick(),self._bg.tick()]): self.update()
    def enterEvent(self,_): self._hov.tgt=1.0
    def leaveEvent(self,_): self._hov.tgt=0.0
    def mousePressEvent(self,e):
        if e.button()==Qt.MouseButton.LeftButton:
            self.seeked.emit(max(0.0,min(1.0,e.position().x()/max(1,self.width()))))
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w=self.width(); h=self._hov.cur; bh=4+3*h; y=(self.height()-bh)/2; r=bh/2
        p.setBrush(QBrush(QColor(self._bg.cur))); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(0,y,w,bh),r,r)
        fw=w*self._prog.cur
        if fw>r:
            ac=QColor(self._acc.cur)
            p.setBrush(QBrush(ac)); p.drawRoundedRect(QRectF(0,y,fw,bh),r,r)
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  Scrolling label
# ══════════════════════════════════════════════════════════════════════════════
class ScrollLabel(QWidget):
    def __init__(self, text="", px=14, bold=False, color=C_FG, parent=None):
        super().__init__(parent); self._text=text; self._px=px; self._bold=bold
        self._col=AnimC(color); self._off=Anim(0,.022)
        self._dir=1; self._pause=100; self._pc=self._pause
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(px+10)
    def set_text(self, t):
        if t!=self._text:
            self._text=t; self._off.cur=0; self._off.tgt=0
            self._dir=1; self._pc=self._pause; self.update()
    def set_col(self, c): self._col.tgt=c
    def set_px(self, px):
        self._px=px; self.setFixedHeight(px+10); self.update()
    def _font(self):
        f=QFont(); f.setPixelSize(max(8,self._px))
        if self._bold: f.setWeight(QFont.Weight.DemiBold)
        return f
    def tick(self):
        dirty=self._col.tick()
        fm=QFontMetrics(self._font()); tw=fm.horizontalAdvance(self._text); aw=self.width()
        if tw>aw+8:
            if self._pc>0: self._pc-=1
            else:
                ms=tw-aw+22
                if self._dir==1:
                    self._off.tgt=ms
                    if abs(self._off.cur-ms)<1.2: self._dir=-1; self._pc=self._pause
                else:
                    self._off.tgt=0
                    if abs(self._off.cur)<1.2: self._dir=1; self._pc=self._pause
        dirty|=self._off.tick()
        if dirty: self.update()
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self._font()); p.setPen(QColor(self._col.cur))
        p.setClipRect(0,0,self.width(),self.height())
        p.drawText(int(-self._off.cur),0,self.width()*4,self.height(),
                   Qt.AlignmentFlag.AlignVCenter,self._text); p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  Settings panel  (right-click popup)
# ══════════════════════════════════════════════════════════════════════════════
class SettingsPanel(QWidget):
    changed = pyqtSignal(dict)
    _PLAT = {"win32":"Windows (SMTC)","darwin":"macOS (Now Playing)",
             "linux":"Linux (MPRIS2)","freebsd":"FreeBSD (MPRIS2)"}

    def __init__(self, settings: dict, accent: QColor, bg: QColor, parent=None):
        super().__init__(parent, Qt.WindowType.Popup|Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._s   = dict(settings)
        # Derive panel palette from album art colours
        self._panel_bg  = bg                       # very dark tinted bg
        self._panel_acc = accent                   # vivid accent
        # Slider fill: accent but less saturated
        h, s, v, _ = accent.getHsvF()
        self._slider_fill = QColor.fromHsvF(h, s * 0.7, min(1.0, v * 0.9))
        self._slider_handle = accent
        # Text colours derived from bg
        self._text_head = QColor.fromHsvF(h, 0.25, 0.75)
        self._text_body = QColor.fromHsvF(h, 0.08, 0.88)
        self._build()

    def _build(self):
        self.setFixedWidth(290)
        root=QVBoxLayout(self); root.setContentsMargins(16,16,16,16); root.setSpacing(9)
        plat=self._PLAT.get(sys.platform, sys.platform)

        # CSS strings derived from palette
        acc_hex   = self._panel_acc.name()
        fill_hex  = self._slider_fill.name()
        head_hex  = self._text_head.name()
        body_hex  = self._text_body.name()
        # Slightly lighter accent for handle
        h,s,v,_ = self._panel_acc.getHsvF()
        hdl_hex = QColor.fromHsvF(h, s*0.6, min(1.0, v*1.2)).name()

        SLIDER_CSS = f"""
            QSlider::groove:horizontal{{background:rgba(0,0,0,90);height:4px;border-radius:2px;}}
            QSlider::handle:horizontal{{background:{hdl_hex};width:12px;height:12px;margin:-4px 0;border-radius:6px;}}
            QSlider::sub-page:horizontal{{background:{fill_hex};border-radius:2px;}}"""
        CB_CSS = f"""
            QCheckBox::indicator{{width:16px;height:16px;border-radius:4px;border:1px solid {fill_hex};}}
            QCheckBox::indicator:checked{{background:{acc_hex};border-color:{hdl_hex};}}"""

        def H(t):
            l=QLabel(t); l.setStyleSheet(f"color:{head_hex};font-size:10px;font-weight:bold;letter-spacing:1px;"); return l
        def L(t):
            l=QLabel(t); l.setStyleSheet(f"color:{body_hex};font-size:12px;"); return l
        def sep():
            f=QFrame(); f.setFrameShape(QFrame.Shape.HLine)
            c=QColor(self._panel_acc); c.setAlpha(40)
            f.setStyleSheet(f"color:{c.name()};"); return f

        def slider(key, lo, hi, scale=100, fmt="{:.0f}%"):
            row=QHBoxLayout(); lbl=L(key.replace("_"," ").title())
            sl=QSlider(Qt.Orientation.Horizontal); sl.setRange(int(lo*scale),int(hi*scale))
            cur=self._s.get(key,0.5)
            sl.setValue(int(cur*scale if scale==100 else cur))
            sl.setFixedWidth(108); sl.setStyleSheet(SLIDER_CSS)
            vl=QLabel(fmt.format(cur*100 if scale==100 else cur))
            vl.setStyleSheet(f"color:{head_hex};font-size:11px;min-width:40px;")
            def on(v,k=key,sc=scale,f=fmt,vl_=vl):
                val=v/sc; self._s[k]=val
                vl_.setText(f.format(val*100 if sc==100 else val)); self.changed.emit(dict(self._s))
            sl.valueChanged.connect(on)
            row.addWidget(lbl); row.addStretch(); row.addWidget(sl); row.addWidget(vl); return row

        def checkbox(key, label):
            row=QHBoxLayout(); lbl=L(label); cb=QCheckBox()
            cb.setChecked(bool(self._s.get(key,False))); cb.setStyleSheet(CB_CSS)
            def on(v,k=key): self._s[k]=bool(v); self.changed.emit(dict(self._s))
            cb.stateChanged.connect(on)
            row.addWidget(lbl); row.addStretch(); row.addWidget(cb); return row

        root.addWidget(H(f"AURORA — {plat.upper()}"))
        root.addWidget(sep())
        root.addWidget(H("APPEARANCE"))
        root.addLayout(slider("opacity",      0.20,1.00,100,"{:.0f}%"))
        root.addLayout(slider("bg_alpha",     40,  255,  1, "{:.0f}"))
        root.addLayout(slider("corner_radius",8,   32,   1, "{:.0f}px"))
        root.addWidget(sep())
        root.addWidget(H("GLOW"))
        root.addLayout(slider("glow_intensity",0.0,1.0,100,"{:.0f}%"))
        root.addLayout(slider("glow_size",     0.2,1.0,100,"{:.0f}%"))
        root.addWidget(sep())
        root.addWidget(H("BEHAVIOUR"))
        root.addLayout(checkbox("always_on_top","Always on top"))
        root.addLayout(checkbox("show_progress","Show progress bar"))
        root.addLayout(checkbox("show_time",    "Show timestamps"))
        root.addWidget(sep())

        # Save button tinted with accent
        dark_acc = QColor.fromHsvF(h, s*0.8, v*0.55).name()
        hover_acc= QColor.fromHsvF(h, s*0.75, min(1.0, v*0.75)).name()
        btn=QPushButton("Save And Close")
        btn.setStyleSheet(f"""QPushButton{{background:{dark_acc};color:#fff;border:none;border-radius:8px;
            padding:7px 16px;font-size:12px;}}QPushButton:hover{{background:{hover_acc};}}
            QPushButton:pressed{{background:{fill_hex};}}""")
        btn.clicked.connect(self._save_close); root.addWidget(btn)
        self.adjustSize()

    def _save_close(self):
        save_cfg(self._s); self.changed.emit(dict(self._s)); self.close()

    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path=QPainterPath(); path.addRoundedRect(QRectF(0,0,self.width(),self.height()),14,14)
        # Background: tinted with album hue, very dark
        bg=QColor(self._panel_bg); bg.setAlpha(252)
        bg2=bg.darker(115); bg2.setAlpha(252)
        g=QLinearGradient(0,0,0,self.height())
        g.setColorAt(0,bg); g.setColorAt(1,bg2)
        p.fillPath(path,QBrush(g))
        # Subtle top shimmer
        sh=QLinearGradient(0,0,0,self.height()*0.3)
        sh.setColorAt(0,QColor(255,255,255,14)); sh.setColorAt(1,QColor(255,255,255,0))
        p.fillPath(path,QBrush(sh))
        # Border: accent-tinted
        bc=QColor(self._panel_acc); bc.setAlpha(55)
        p.setPen(QPen(bc,1)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(.5,.5,self.width()-1,self.height()-1),14,14); p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  Resize edge detection
# ══════════════════════════════════════════════════════════════════════════════
class Edge:
    NONE=0; L=1; R=2; T=4; B=8
    TL=T|L; TR=T|R; BL=B|L; BR=B|R

    @staticmethod
    def detect(pos: QPoint, w: int, h: int, gp: int) -> int:
        # pos is widget-local (includes GLOW_PAD margin)
        x,y = pos.x()-gp, pos.y()-gp   # relative to card
        e=Edge.NONE; ch=CORNER_HIT; eh=EDGE_HIT
        near_l=(0<=x<=eh); near_r=(w-eh<=x<=w)
        near_t=(0<=y<=eh); near_b=(h-eh<=y<=h)
        corner_l=(0<=x<=ch); corner_r=(w-ch<=x<=w)
        corner_t=(0<=y<=ch); corner_b=(h-ch<=y<=h)
        if corner_l and corner_t: return Edge.TL
        if corner_r and corner_t: return Edge.TR
        if corner_l and corner_b: return Edge.BL
        if corner_r and corner_b: return Edge.BR
        if near_l: e|=Edge.L
        if near_r: e|=Edge.R
        if near_t: e|=Edge.T
        if near_b: e|=Edge.B
        return e

    @staticmethod
    def cursor(e: int) -> Qt.CursorShape:
        C=Qt.CursorShape
        return {Edge.TL:C.SizeFDiagCursor, Edge.BR:C.SizeFDiagCursor,
                Edge.TR:C.SizeBDiagCursor, Edge.BL:C.SizeBDiagCursor,
                Edge.L:C.SizeHorCursor,   Edge.R:C.SizeHorCursor,
                Edge.T:C.SizeVerCursor,   Edge.B:C.SizeVerCursor}.get(e, C.ArrowCursor)


# ══════════════════════════════════════════════════════════════════════════════
#  Main widget
# ══════════════════════════════════════════════════════════════════════════════
class MusicWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._cfg       = load_cfg()
        self._glow_ph   = 0.0
        self._drag      = None   # QPoint offset when moving
        self._resize_edge=Edge.NONE
        self._resize_origin: Optional[QPoint] = None
        self._resize_start_geom: Optional[QRect] = None
        self._setup_window()
        self._setup_palette()
        self._build_ui()
        self._worker=MediaWorker(); self._worker.updated.connect(self._on_update); self._worker.start()
        self._timer=QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(1000//FPS)

    # ── window setup ──────────────────────────────────────────────────────────
    def _setup_window(self):
        flags=Qt.WindowType.FramelessWindowHint
        if self._cfg.get("always_on_top"): flags|=Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self.setWindowTitle("Aurora Music Widget")

        cw=max(MIN_W, int(self._cfg.get("win_w",DEFAULT_W)))
        ch=max(MIN_H, int(self._cfg.get("win_h",DEFAULT_H)))
        total_w=cw+GLOW_PAD*2; total_h=ch+GLOW_PAD*2
        self.setMinimumSize(MIN_W+GLOW_PAD*2, MIN_H+GLOW_PAD*2)
        self.resize(total_w, total_h)

        sx,sy=int(self._cfg.get("win_x",-1)),int(self._cfg.get("win_y",-1))
        if sx<0 or sy<0:
            screen=QApplication.primaryScreen().geometry()
            self.move(screen.width()-total_w-30, screen.height()-total_h-50)
        else:
            self.move(sx,sy)

    # ── palette state ─────────────────────────────────────────────────────────
    def _setup_palette(self):
        self._track=Track(); self._player=""
        self._art_px: Optional[QPixmap]=None; self._art_cache: dict={}
        self._bg    =AnimC(C_BG,.04);  self._acc=AnimC(C_ACC,.04)
        self._bg_mid=AnimC(C_BG,.04);  self._bg_bot=AnimC(C_BG,.04)
        self._fg=AnimC(C_FG,.05);   self._mut=AnimC(C_MUT,.05)
        self._pb_bg=AnimC(C_PROG,.04); self._glow_c=AnimC(C_ACC,.035)
        self._prog=Anim(0,.05); self._spin=0.0
        self._fade=Anim(0,.06); self._fade.tgt=1.0
        self._t_cur="0:00"; self._t_tot="0:00"

    # ── build child widgets ────────────────────────────────────────────────────
    def _build_ui(self):
        self._title =ScrollLabel("No media",15,True, C_FG, self)
        self._artist=ScrollLabel("",        11,False,C_MUT,self)
        self._pbar  =ProgBar(self); self._pbar.seeked.connect(self._on_seek)
        if not self._cfg.get("show_progress",True): self._pbar.hide()

        self._btn_prev=IconBtn(_prev,44,False,self)
        self._btn_play=IconBtn(_play,56,True, self)
        self._btn_next=IconBtn(_next,44,False,self)
        self._btn_prev.clicked.connect(lambda:self._cmd("Previous"))
        self._btn_play.clicked.connect(lambda:self._cmd("PlayPause"))
        self._btn_next.clicked.connect(lambda:self._cmd("Next"))

        self._relayout()

    def _card_rect(self) -> tuple[int,int,int,int]:
        """Return (card_x, card_y, card_w, card_h) — card inside the window."""
        return GLOW_PAD, GLOW_PAD, self.width()-GLOW_PAD*2, self.height()-GLOW_PAD*2

    def _relayout(self):
        """Reposition and resize all child widgets proportionally to card size."""
        cx, cy, cw, ch = self._card_rect()

        # Scale factors relative to default size
        sx = cw / DEFAULT_W
        sy = ch / DEFAULT_H

        # Art size: proportional, square, never larger than card
        art_size = int(min(cw - 16, ch * 0.53))
        art_pad  = (cw - art_size) // 2
        art_top  = int(14 * sy)

        # Font sizes
        title_px  = max(10, int(15 * sx))
        artist_px = max(8,  int(11 * sx))

        # Vertical layout below art
        y = cy + art_top + art_size + int(14 * sy)

        self._title.set_px(title_px)
        self._artist.set_px(artist_px)
        self._title.setGeometry (cx+art_pad, y, art_size, title_px+10);  y += title_px+10
        self._artist.setGeometry(cx+art_pad, y, art_size, artist_px+10); y += artist_px+10+int(10*sy)

        if self._cfg.get("show_progress",True):
            self._pbar.setGeometry(cx+art_pad, y, art_size, 18); y+=18+int(4*sy)
        self._time_y  = y;  y += 15 + int(12*sy)

        # Buttons — scale proportionally
        play_sz = max(36, int(56 * sx))
        side_sz = max(28, int(44 * sx))
        gap     = max(8,  int(14 * sx))
        total_b = side_sz + gap + play_sz + gap + side_sz
        bx      = cx + (cw - total_b) // 2

        self._btn_prev.resize_to(side_sz); self._btn_prev.move(bx,              y)
        self._btn_play.resize_to(play_sz); self._btn_play.move(bx+side_sz+gap,  y - (play_sz-side_sz)//2)
        self._btn_next.resize_to(side_sz); self._btn_next.move(bx+side_sz+gap+play_sz+gap, y)

        # Store for paintEvent
        self._art_rect  = (cx+art_pad, cy+art_top, art_size, art_size)
        self._art_radius= max(8, int(14*sx))

    # ── frame tick ────────────────────────────────────────────────────────────
    def _tick(self):
        self._title.tick(); self._artist.tick(); self._pbar.tick()
        for b in (self._btn_prev,self._btn_play,self._btn_next): b.tick()
        self._spin=(self._spin+0.22)%360
        self._glow_ph=(self._glow_ph+0.018)%(math.pi*2)
        dirty=any([self._bg.tick(),self._bg_mid.tick(),self._bg_bot.tick(),
                   self._acc.tick(),self._fg.tick(),self._mut.tick(),
                   self._pb_bg.tick(),self._glow_c.tick(),self._prog.tick(),self._fade.tick()])
        if self._track.playing and self._track.duration>0:
            self._track.position=min(self._track.duration,
                                     self._track.position+int(1_000_000/FPS))
            self._prog.tgt=self._track.progress()
            self._pbar.set_prog(self._prog.cur)
            self._t_cur=self._track.fmt(self._track.position); dirty=True
        if dirty:
            self.setWindowOpacity(self._cfg.get("opacity",0.95))
            self._push_pal(); self.update()

    def _push_pal(self):
        ac=self._acc.cur; fg=self._fg.cur; mu=self._mut.cur
        self._pbar.set_acc(ac); self._pbar._bg.tgt=self._pb_bg.cur
        self._title.set_col(fg); self._artist.set_col(mu)
        for b in (self._btn_prev,self._btn_next): b.set_acc(ac); b.set_fg(mu)
        self._btn_play.set_acc(ac); self._btn_play.set_fg(fg)

    # ── media update ──────────────────────────────────────────────────────────
    def _on_update(self, tr: Track):
        new=(tr.title!=self._track.title or tr.artist!=self._track.artist)
        self._player=tr.player; self._track=tr
        self._title.set_text(tr.title); self._artist.set_text(tr.artist)
        self._prog.tgt=tr.progress(); self._pbar.set_prog(self._prog.cur)
        self._t_cur=tr.fmt(tr.position); self._t_tot=tr.fmt(tr.duration)
        self._btn_play.draw=_pause if tr.playing else _play
        if tr.art_bytes:
            key=tr.art_url or id(tr.art_bytes)
            fresh_art=(key not in self._art_cache)
            if fresh_art:
                px=QPixmap(); px.loadFromData(tr.art_bytes); self._art_cache[key]=px
            self._art_px=self._art_cache[key]
            # Re-extract whenever either track metadata or art itself is new
            if new or fresh_art:
                snap=fresh_art   # snap colours instantly when art is truly new
                threading.Thread(target=self._do_extract,
                                 args=(tr.art_bytes,snap),daemon=True).start()
        else:
            self._art_px=None

    def _do_extract(self, img_bytes, snap: bool=False):
        pal=extract_palette(img_bytes)
        if not pal: return
        self._bg.tgt    = pal["bg"];   self._bg_mid.tgt = pal["bg_mid"]
        self._bg_bot.tgt= pal["bg_bot"]; self._acc.tgt  = pal["acc"]
        self._fg.tgt    = pal["fg"];   self._mut.tgt    = pal["muted"]
        self._pb_bg.tgt = pal["prog_bg"]; self._glow_c.tgt= pal["glow"]
        if snap:
            # Instant colour reset so there's no bleed from previous track
            for anim, key in [(self._bg,"bg"),(self._bg_mid,"bg_mid"),
                              (self._bg_bot,"bg_bot"),(self._acc,"acc"),
                              (self._glow_c,"glow")]:
                anim.cur = QColor(pal[key])

    def _on_seek(self,f): self._track.position=int(f*self._track.duration); self._worker.seek(self._player,f)
    def _cmd(self,c): self._worker.cmd(self._player,c)

    def _apply_cfg(self, s: dict):
        self._cfg=s
        flags=Qt.WindowType.FramelessWindowHint
        if s.get("always_on_top"): flags|=Qt.WindowType.WindowStaysOnTopHint
        vis=self.isVisible(); self.setWindowFlags(flags)
        if vis: self.show()
        self._pbar.setVisible(bool(s.get("show_progress",True)))
        self._relayout(); self.update()

    # ── paint ─────────────────────────────────────────────────────────────────
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        cx,cy,cw,ch = self._card_rect()
        s=self._cfg

        # ── glow — uniform outline bloom ──────────────────────────────────────
        # Strokes the card shape outward so every edge and corner glows equally.
        gi = float(s.get("glow_intensity", 0.75))
        if gi > 0.02:
            gs      = float(s.get("glow_size", 0.65))
            gcol    = QColor(self._glow_c.cur)
            breath  = 0.85 + 0.15 * math.sin(self._glow_ph)
            eff     = gi * breath
            layers  = max(4, int(GLOW_PAD * gs))
            cr_base = int(s.get("corner_radius", 22))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(layers, 0, -1):
                # quadratic falloff: brightest just outside the card, soft at far edge
                t   = 1.0 - i / (layers + 1)
                a   = int(eff * 115 * (1 - t) ** 1.5)
                if a < 1: continue
                gc  = QColor(gcol); gc.setAlpha(a)
                exp = layers - i   # px outside card edge this ring sits
                p.setPen(QPen(gc, 2.0))
                p.drawRoundedRect(
                    QRectF(cx - exp, cy - exp, cw + exp * 2, ch + exp * 2),
                    cr_base + exp * 0.7,
                    cr_base + exp * 0.7,
                )

        # ── shadow ────────────────────────────────────────────────────────────
        cr=int(s.get("corner_radius",22))
        for i in range(20,0,-1):
            sc=QColor(0,0,0,int(7*(i/20)**1.9))
            p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(sc,1))
            p.drawRoundedRect(QRectF(cx+i,cy+i,cw-2*i,ch-2*i),cr+i*.4,cr+i*.4)

        # ── card — album art gradient background ──────────────────────────────
        card=QPainterPath(); card.addRoundedRect(QRectF(cx,cy,cw,ch),cr,cr)
        bg_a=int(s.get("bg_alpha",225))
        c_top= QColor(self._bg.cur);     c_top.setAlpha(bg_a)
        c_mid= QColor(self._bg_mid.cur); c_mid.setAlpha(bg_a)
        c_bot= QColor(self._bg_bot.cur); c_bot.setAlpha(bg_a)
        gg=QLinearGradient(cx,cy,cx,cy+ch)
        gg.setColorAt(0.00, c_top)
        gg.setColorAt(0.50, c_mid)
        gg.setColorAt(1.00, c_bot)
        p.fillPath(card,QBrush(gg))
        # Subtle top shimmer
        sh=QLinearGradient(cx,cy,cx,cy+ch*.32)
        sh.setColorAt(0,QColor(255,255,255,18)); sh.setColorAt(1,QColor(255,255,255,0))
        p.fillPath(card,QBrush(sh))
        # Accent border
        bc=QColor(self._acc.cur); bc.setAlpha(50)
        p.setPen(QPen(bc,1)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx+.5,cy+.5,cw-1,ch-1),cr,cr)

        # ── album art ─────────────────────────────────────────────────────────
        ax,ay,art_sz,_ = self._art_rect
        ar=self._art_radius
        art_path=QPainterPath(); art_path.addRoundedRect(QRectF(ax,ay,art_sz,art_sz),ar,ar)
        p.setClipPath(art_path)

        if self._art_px and not self._art_px.isNull():
            pw=self._art_px.width(); ph=self._art_px.height()
            sc=max(art_sz/max(pw,1),art_sz/max(ph,1))
            scaled=self._art_px.scaled(int(pw*sc),int(ph*sc),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int(ax+(art_sz-scaled.width())//2),int(ay+(art_sz-scaled.height())//2),scaled)
        else:
            p.fillRect(ax,ay,art_sz,art_sz,QColor(self._bg.cur).lighter(140))
            p.setClipping(False); p.save()
            p.translate(ax+art_sz/2,ay+art_sz/2)
            if self._track.playing: p.rotate(self._spin)
            fs=max(24,int(art_sz*.30)); nf=QFont(); nf.setPixelSize(fs); p.setFont(nf)
            nc=QColor(self._acc.cur); nc.setAlpha(100); p.setPen(nc)
            p.drawText(QRectF(-fs,-fs,fs*2,fs*2),Qt.AlignmentFlag.AlignCenter,"♪")
            p.restore()

        # art vignette
        p.setClipPath(art_path)
        vg=QLinearGradient(ax,ay,ax,ay+art_sz)
        vg.setColorAt(0.0,QColor(0,0,0,0)); vg.setColorAt(.72,QColor(0,0,0,0)); vg.setColorAt(1.0,QColor(0,0,0,100))
        p.fillRect(QRectF(ax,ay,art_sz,art_sz),QBrush(vg)); p.setClipping(False)

        # ── time labels ───────────────────────────────────────────────────────
        if s.get("show_time",True):
            art_pad=(cw-art_sz)//2
            tf=QFont(); tf.setPixelSize(max(8,int(10*(cw/DEFAULT_W)))); p.setFont(tf)
            mc=QColor(self._mut.cur); mc.setAlpha(150); p.setPen(mc)
            p.drawText(QRectF(cx+art_pad,self._time_y,art_sz//2,15),
                       Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,self._t_cur)
            p.drawText(QRectF(cx+art_pad+art_sz//2,self._time_y,art_sz//2,15),
                       Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,self._t_tot)
        p.end()

    # ── resize: handle events ─────────────────────────────────────────────────
    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self,"_title"): self._relayout()

    def _hit_edge(self, pos: QPoint) -> int:
        cx,cy,cw,ch = self._card_rect()
        return Edge.detect(pos, cw, ch, GLOW_PAD)

    def mousePressEvent(self, e):
        if e.button()==Qt.MouseButton.LeftButton:
            edge=self._hit_edge(e.position().toPoint())
            if edge!=Edge.NONE:
                self._resize_edge=edge
                self._resize_origin=e.globalPosition().toPoint()
                self._resize_start_geom=self.geometry()
            else:
                self._drag=e.globalPosition().toPoint()-self.frameGeometry().topLeft()
        elif e.button()==Qt.MouseButton.RightButton:
            self._open_settings(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        pos=e.position().toPoint()
        if self._resize_edge!=Edge.NONE and self._resize_origin and self._resize_start_geom:
            self._do_resize(e.globalPosition().toPoint())
        elif self._drag and e.buttons()==Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self._drag)
        else:
            # Update cursor based on hover zone
            edge=self._hit_edge(pos)
            self.setCursor(Edge.cursor(edge))

    def mouseReleaseEvent(self, e):
        if e.button()==Qt.MouseButton.LeftButton:
            if self._resize_edge!=Edge.NONE:
                # Save new size to config
                cw=self.width()-GLOW_PAD*2; ch=self.height()-GLOW_PAD*2
                self._cfg["win_w"]=cw; self._cfg["win_h"]=ch
                save_cfg(self._cfg)
            self._drag=None; self._resize_edge=Edge.NONE
            self._resize_origin=None; self._resize_start_geom=None

    def _do_resize(self, gpos: QPoint):
        if not self._resize_start_geom or not self._resize_origin: return
        dx=gpos.x()-self._resize_origin.x()
        dy=gpos.y()-self._resize_origin.y()
        g=self._resize_start_geom; e=self._resize_edge
        x,y,w,h = g.x(),g.y(),g.width(),g.height()
        min_tw=MIN_W+GLOW_PAD*2; min_th=MIN_H+GLOW_PAD*2

        if e & Edge.R: w=max(min_tw, g.width()+dx)
        if e & Edge.B: h=max(min_th, g.height()+dy)
        if e & Edge.L:
            new_w=max(min_tw, g.width()-dx)
            x=g.right()-new_w; w=new_w
        if e & Edge.T:
            new_h=max(min_th, g.height()-dy)
            y=g.bottom()-new_h; h=new_h

        self.setGeometry(x,y,w,h)

    def _open_settings(self, gpos: QPoint):
        panel=SettingsPanel(self._cfg, self._acc.cur, self._bg.cur, self)
        panel.changed.connect(self._apply_cfg)
        screen=QApplication.primaryScreen().geometry()
        sx=max(0,min(gpos.x()-panel.width()-8, screen.width()-panel.width()))
        sy=max(0,min(gpos.y(), screen.height()-panel.height()-20))
        panel.move(sx,sy); panel.show()

    def keyPressEvent(self, e):
        k=e.key()
        if   k==Qt.Key.Key_Escape: self.close()
        elif k==Qt.Key.Key_Space:  self._cmd("PlayPause")
        elif k==Qt.Key.Key_Right:  self._cmd("Next")
        elif k==Qt.Key.Key_Left:   self._cmd("Previous")

    def closeEvent(self, e):
        # Save window position and size
        pos=self.pos()
        self._cfg["win_x"]=pos.x(); self._cfg["win_y"]=pos.y()
        self._cfg["win_w"]=self.width()-GLOW_PAD*2
        self._cfg["win_h"]=self.height()-GLOW_PAD*2
        save_cfg(self._cfg)
        self._worker.stop(); self._worker.wait(2000); e.accept()


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════
def main():
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    app=QApplication(sys.argv)
    app.setApplicationName("Aurora Music Widget")
    app.setOrganizationName("aurora")
    app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    w=MusicWidget(); w.show()
    sys.exit(app.exec())

if __name__=="__main__": main()
