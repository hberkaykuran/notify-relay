"""DirectComposition through ctypes: one click-through popup window whose content
is composed and animated by DWM on the GPU, at each monitor's own refresh rate.

Why: the relay's cards used to be Tk windows moved by a Tk timer. That capped
motion at the timer (60 fps at best on 165/240 Hz screens), resizing them
flashed black, and mapping them took focus. Here Python only describes an
animation (a cubic per property) and commits it; DWM renders every frame.

The vtable indices below are fixed by dcomp.h / d3d11.h. MSVC orders overloaded
virtuals in reverse, so every animatable property has the animation overload at
index n and the plain value at n+1 (SetOffsetX(anim)=3, SetOffsetX(float)=4).

Every COM call goes through Compositor.call(), so a lost device (driver update,
GPU reset, resume) surfaces as one OSError the overlay can rebuild from, and
the test hook lose_device() fakes exactly that without touching a pointer.
"""

import ctypes
import uuid
from ctypes import wintypes as W

from PIL import Image, ImageChops

c_void_p, HRESULT = ctypes.c_void_p, ctypes.HRESULT
DEVICE_REMOVED = 0x887A0005 - (1 << 32)        # DXGI_ERROR_DEVICE_REMOVED as a signed HRESULT

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
gdi32 = ctypes.WinDLL("gdi32")

kernel32.GetModuleHandleW.restype = W.HMODULE
user32.CreateWindowExW.restype = W.HWND
user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, c_void_p]
user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT]
user32.SetWindowRgn.argtypes = [W.HWND, W.HANDLE, W.BOOL]
user32.LoadCursorW.restype = W.HANDLE
user32.LoadCursorW.argtypes = [W.HINSTANCE, c_void_p]
gdi32.CreateRectRgn.restype = W.HANDLE
gdi32.CombineRgn.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, ctypes.c_int]
gdi32.DeleteObject.argtypes = [W.HANDLE]


class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]


def _guid(s):
    return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


IID_IDXGIDevice = _guid("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_DesktopDevice = _guid("5f4633fe-1e08-4cb8-8c75-ce24333f5602")
IID_Texture2D = _guid("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


class BOX(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint) for n in ("left", "top", "front", "right", "bottom", "back")]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("dwFlags", W.DWORD), ("hwndTrack", W.HWND),
                ("dwHoverTime", W.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("proc", WNDPROC), ("cls", ctypes.c_int), ("wnd", ctypes.c_int),
                ("inst", W.HINSTANCE), ("icon", W.HICON), ("cursor", W.HANDLE), ("bg", W.HBRUSH),
                ("menu", W.LPCWSTR), ("name", W.LPCWSTR)]


WS_POPUP = 0x80000000
WS_EX = 0x00200000 | 0x8 | 0x80 | 0x08000000   # NOREDIRECTIONBITMAP | TOPMOST | TOOLWINDOW | NOACTIVATE
SWP_NOACTIVATE, SWP_NOZORDER = 0x10, 0x4
_classes = {}                                  # registered window class -> its WNDPROC (kept alive)
_protos = {}                                   # argtypes -> cached WINFUNCTYPE


def ease_value(a, b, t):
    """Ease-out cubic: the curve the compositor animations below run."""
    t = min(1.0, max(0.0, t))
    return a + (b - a) * (1 - (1 - t) ** 3)


def cubic_terms(a, b, duration):
    """(constant, linear, quadratic, cubic) of ease_value over `duration` seconds:
    a + k(3s - 3s^2 + s^3) with s = t/T, expanded in t."""
    k = b - a
    return a, 3 * k / duration, -3 * k / duration ** 2, k / duration ** 3


class Compositor:
    def __init__(self, rect, on_message):
        """A hidden-until-shown popup covering `rect` (screen coords) and a DComp tree on it.
        on_message(msg, wparam, lparam) -> int | None handles window messages."""
        self.lost = False
        self.objects = []                      # every COM pointer we own, released in reverse
        d3d11, dcomp = ctypes.WinDLL("d3d11"), ctypes.WinDLL("dcomp")
        dev, ctx, fl = c_void_p(), c_void_p(), ctypes.c_uint()
        hr = d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7, ctypes.byref(dev),
                                     ctypes.byref(fl), ctypes.byref(ctx))
        if hr or not dev.value:
            raise OSError(hr, "D3D11CreateDevice failed")
        self.dev, self.ctx = dev.value, ctx.value
        self.objects += [self.dev, self.ctx]
        self.dxgi = self._out(self.dev, 0, ctypes.POINTER(GUID), ctypes.byref(IID_IDXGIDevice))
        dcomp.DCompositionCreateDevice3.argtypes = [c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(c_void_p)]
        dc = c_void_p()
        hr = dcomp.DCompositionCreateDevice3(self.dxgi, ctypes.byref(IID_DesktopDevice), ctypes.byref(dc))
        if hr or not dc.value:
            raise OSError(hr, "DCompositionCreateDevice3 failed")
        self.dc = dc.value
        self.objects.append(self.dc)

        self._proc = WNDPROC(lambda h, m, wp, lp: self._wndproc(h, m, wp, lp, on_message))
        name = f"notify-relay-dcomp-{id(self)}"
        wc = WNDCLASS(0, self._proc, 0, 0, kernel32.GetModuleHandleW(None), None,
                      user32.LoadCursorW(None, 32512), None, None, name)       # IDC_ARROW
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
        _classes[name] = self._proc
        self.class_name = name
        left, top, right, bottom = rect
        self.hwnd = user32.CreateWindowExW(WS_EX, name, "", WS_POPUP, left, top, right - left,
                                           bottom - top, None, None, wc.inst, None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        self.origin = (left, top)
        self.set_region([])                    # nothing clickable until there are cards
        user32.ShowWindow(self.hwnd, 4)        # SW_SHOWNOACTIVATE: appears without taking focus

        self.target = self.call(self.dc, 24, (W.HWND, W.BOOL, ctypes.POINTER(c_void_p)),
                                self.hwnd, True, out=True)
        self.root = self.call(self.dc, 6, (ctypes.POINTER(c_void_p),), out=True)
        self.call(self.target, 3, (c_void_p,), self.root)
        self.commit()

    # --- plumbing ---
    @staticmethod
    def _wndproc(h, msg, wp, lp, on_message):
        if msg == 0x0021:                      # WM_MOUSEACTIVATE: never activate on click
            return 3                           # MA_NOACTIVATE
        try:
            r = on_message(msg, wp, lp)
        except Exception:
            r = None
        return r if r is not None else user32.DefWindowProcW(h, msg, wp, lp)

    def call(self, obj, idx, argtypes, *args, out=False):
        """COM method `idx` of `obj`. With out=True the last arg is an out-pointer we own."""
        if self.lost:
            raise OSError(DEVICE_REMOVED, "device removed")
        vt = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents
        proto = _protos.get(argtypes)
        if proto is None:                      # building a prototype costs more than the call
            proto = _protos[argtypes] = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)
        fn = proto(vt[idx])
        if not out:
            return fn(obj, *args)
        p = c_void_p()
        fn(obj, *args, ctypes.byref(p))
        self.objects.append(p.value)
        return p.value

    def _out(self, obj, idx, *argtypes_and_arg):
        argtype, arg = argtypes_and_arg
        return self.call(obj, idx, (argtype, ctypes.POINTER(c_void_p)), arg, out=True)

    def release(self, obj):
        if obj in self.objects:
            self.objects.remove(obj)
            try:
                ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(
                    ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents[2])(obj)
            except OSError:
                pass

    def lose_device(self):
        """Test hook: make the next call fail exactly as a removed device does."""
        self.lost = True

    def close(self):
        for obj in reversed(list(self.objects)):
            self.release(obj)
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        user32.UnregisterClassW(self.class_name, kernel32.GetModuleHandleW(None))
        _classes.pop(self.class_name, None)

    def commit(self):
        self.call(self.dc, 3, ())

    # --- window ---
    def move_window(self, rect):
        left, top, right, bottom = rect
        self.origin = (left, top)
        user32.SetWindowPos(self.hwnd, None, left, top, right - left, bottom - top,
                            SWP_NOACTIVATE | SWP_NOZORDER)

    def track_leave(self):
        """Ask for one WM_MOUSELEAVE when the pointer leaves the window's region."""
        user32.TrackMouseEvent(ctypes.byref(TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 2,
                                                            self.hwnd, 0)))

    def set_region(self, rects):
        """Only these window-relative rects draw and take clicks; the rest is click-through."""
        rgn = gdi32.CreateRectRgn(0, 0, 0, 0)
        for (l, t, r, b) in rects:
            part = gdi32.CreateRectRgn(int(l), int(t), int(r), int(b))
            gdi32.CombineRgn(rgn, rgn, part, 2)          # RGN_OR
            gdi32.DeleteObject(part)
        # redraw=FALSE: DComp content needs no GDI repaint, and a synchronous one is the
        # costliest thing a settle would otherwise do. The window now owns rgn.
        user32.SetWindowRgn(self.hwnd, rgn, False)

    # --- content ---
    def surface(self, image):
        """Upload a Pillow RGBA image as a DComp surface (premultiplied BGRA)."""
        w, h = image.size
        r, g, b, a = image.convert("RGBA").split()
        pm = [ImageChops.multiply(ch, a) for ch in (b, g, r)]   # premultiplied, BGR order
        data = bytearray(Image.merge("RGBA", (*pm, a)).tobytes())
        surf = self.call(self.dc, 8, (ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                      ctypes.POINTER(c_void_p)), w, h, 87, 1, out=True)
        tex, off = c_void_p(), W.POINT()
        self.call(surf, 3, (c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(c_void_p),
                            ctypes.POINTER(W.POINT)),
                  None, ctypes.byref(IID_Texture2D), ctypes.byref(tex), ctypes.byref(off))
        box = BOX(off.x, off.y, 0, off.x + w, off.y + h, 1)
        buf = (ctypes.c_char * len(data)).from_buffer(data)
        vt = ctypes.cast(self.ctx, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(None, c_void_p, c_void_p, ctypes.c_uint, ctypes.POINTER(BOX), c_void_p,
                           ctypes.c_uint, ctypes.c_uint)(vt[48])(
            self.ctx, tex.value, 0, ctypes.byref(box), ctypes.addressof(buf), w * 4, 0)
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(
            ctypes.cast(tex.value, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents[2])(tex.value)
        self.call(surf, 4, ())                           # EndDraw
        return surf

    def animation(self, a, b, duration):
        an = self.call(self.dc, 23, (ctypes.POINTER(c_void_p),), out=True)
        c0, c1, c2, c3 = cubic_terms(a, b, duration)
        self.call(an, 5, (ctypes.c_double, ctypes.c_float, ctypes.c_float, ctypes.c_float,
                          ctypes.c_float), 0.0, c0, c1, c2, c3)
        self.call(an, 8, (ctypes.c_double, ctypes.c_float), duration, b)
        return an


class Prop:
    """One animatable float: knows its value at any moment, so a retarget mid-flight
    starts exactly where the compositor currently is."""

    def __init__(self, value):
        self.a = self.b = value
        self.t0 = 0.0
        self.dur = 0.0

    def now(self, clock):
        if not self.dur:
            return self.b
        return ease_value(self.a, self.b, (clock - self.t0) / self.dur)


class Visual:
    """A card on the compositor: content surface, offset, opacity, scale, bottom clip."""

    def __init__(self, comp):
        self.comp = comp
        c = comp.call
        self.v = c(comp.dc, 6, (ctypes.POINTER(c_void_p),), out=True)
        self.fx = c(comp.dc, 21, (ctypes.POINTER(c_void_p),), out=True)       # effect group
        self.scale = c(comp.dc, 11, (ctypes.POINTER(c_void_p),), out=True)    # scale transform
        self.clip = c(comp.dc, 22, (ctypes.POINTER(c_void_p),), out=True)     # rectangle clip
        c(self.v, 10, (c_void_p,), self.fx)                                    # SetEffect
        c(self.v, 7, (c_void_p,), self.scale)                                  # SetTransform
        c(self.v, 13, (c_void_p,), self.clip)                                  # SetClip
        # AddVisual with no reference visual: insertAbove=FALSE puts it on top of its
        # siblings, TRUE at the bottom (the reverse of what the name suggests).
        c(comp.root, 16, (c_void_p, W.BOOL, c_void_p), self.v, False, None)
        self.surface = None
        self.props = {k: Prop(v) for k, v in
                      (("x", 0.0), ("y", 0.0), ("opacity", 0.0), ("scale", 1.0), ("clip", 1e6))}
        self.children = []

    def set_content(self, surface):
        self.comp.call(self.v, 15, (c_void_p,), surface)
        self.surface = surface

    def set_static(self, cx, cy, clip_w):
        """Scale centre (top middle of the card) and the clip's fixed edges."""
        c = self.comp.call
        c(self.scale, 8, (ctypes.c_float,), cx)                # SetCenterX(float)
        c(self.scale, 10, (ctypes.c_float,), cy)               # SetCenterY(float)
        c(self.clip, 4, (ctypes.c_float,), 0.0)                # SetLeft(float)
        c(self.clip, 6, (ctypes.c_float,), 0.0)                # SetTop(float)
        c(self.clip, 8, (ctypes.c_float,), clip_w)             # SetRight(float)

    def go(self, clock, duration, **targets):
        """Animate each named property from where it is now to its target."""
        c = self.comp.call
        for name, target in targets.items():
            p = self.props[name]
            start = p.now(clock)
            settled = not p.dur or clock >= p.t0 + p.dur
            if settled and abs(start - target) < 0.01 and p.t0:
                continue                       # already there and not moving: no COM calls
            p.a, p.b, p.t0, p.dur = start, float(target), clock, duration if duration else 0.0
            setters = {"x": [(self.v, 3, 4)], "y": [(self.v, 5, 6)], "opacity": [(self.fx, 3, 4)],
                       "scale": [(self.scale, 3, 4), (self.scale, 5, 6)], "clip": [(self.clip, 9, 10)]}
            for obj, anim_idx, value_idx in setters[name]:
                if duration and abs(start - target) > 0.01:
                    an = self.comp.animation(start, float(target), duration)
                    c(obj, anim_idx, (c_void_p,), an)
                    self.comp.release(an)            # the property holds its own reference
                else:
                    c(obj, value_idx, (ctypes.c_float,), float(target))

    def child(self, surface, x, y):
        """A small fixed sub-visual (close glyph, "+N more") that moves with the card."""
        c = self.comp.call
        ch = c(self.comp.dc, 6, (ctypes.POINTER(c_void_p),), out=True)
        fx = c(self.comp.dc, 21, (ctypes.POINTER(c_void_p),), out=True)
        c(ch, 10, (c_void_p,), fx)
        c(ch, 15, (c_void_p,), surface)
        c(ch, 4, (ctypes.c_float,), float(x))
        c(ch, 6, (ctypes.c_float,), float(y))
        c(self.v, 16, (c_void_p, W.BOOL, c_void_p), ch, True, None)
        entry = {"visual": ch, "fx": fx, "surface": surface, "opacity": Prop(1.0)}
        self.children.append(entry)
        return entry

    def child_opacity(self, entry, value, clock, duration=0.12):
        p = entry["opacity"]
        start = p.now(clock)
        p.a, p.b, p.t0, p.dur = start, value, clock, duration
        if duration and abs(start - value) > 0.01:
            an = self.comp.animation(start, value, duration)
            self.comp.call(entry["fx"], 3, (c_void_p,), an)
            self.comp.release(an)
        else:
            self.comp.call(entry["fx"], 4, (ctypes.c_float,), float(value))

    def remove_child(self, entry):
        self.comp.call(self.v, 17, (c_void_p,), entry["visual"])
        for k in ("visual", "fx", "surface"):
            self.comp.release(entry[k])
        self.children.remove(entry)

    def destroy(self):
        c = self.comp.call
        c(self.comp.root, 17, (c_void_p,), self.v)              # RemoveVisual
        for entry in list(self.children):
            self.remove_child(entry)
        for obj in (self.v, self.fx, self.scale, self.clip, self.surface):
            if obj:
                self.comp.release(obj)
