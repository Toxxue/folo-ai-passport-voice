#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interception 驱动级按键注入后端。

通过官方 interception.dll 调用内核过滤驱动（keyboard.sys）发送按键，
事件在驱动层生成，不携带 LLMHF_INJECTED 标记，因此对"只认真实键盘"的
语音热键（如豆包输入法长按右 Alt）有效。

依赖：
  1. 已安装 Interception 过滤驱动（install_interception.ps1，重启生效）；
  2. x64 interception.dll（默认在本文件同目录或 dll 子目录）。

键位复用 hotkey_forwarder 解析出的元组：(name, vk, scan, extended)，
其中 scan 为 PS/2 set-1 扫描码，extended 决定是否加 E0 前缀。
"""

import ctypes
import os
import sys
import time

# ---- Interception 枚举（见 interception.h） ----
INTERCEPTION_KEY_DOWN = 0x00
INTERCEPTION_KEY_UP = 0x01
INTERCEPTION_KEY_E0 = 0x02
INTERCEPTION_KEY_E1 = 0x04

INTERCEPTION_MAX_KEYBOARD = 10


class KeyStroke(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort),
                ("state", ctypes.c_ushort),
                ("information", ctypes.c_uint)]


def _find_dll():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "interception.dll"),
        os.path.join(here, "dll", "interception.dll"),
        os.path.join(here, "..", "dll", "interception.dll"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return "interception.dll"   # 退回系统 PATH 查找


class InterceptionError(RuntimeError):
    pass


class InterceptionBackend:
    """与 hotkey_forwarder.KeyInjector 对接的驱动级后端。"""

    def __init__(self, dll_path=None):
        if sys.platform != "win32":
            raise InterceptionError("Interception 后端仅支持 Windows")
        path = dll_path or _find_dll()
        try:
            self._dll = ctypes.WinDLL(path)
        except OSError as e:
            raise InterceptionError(f"无法加载 interception.dll: {e}")

        d = self._dll
        d.interception_create_context.restype = ctypes.c_void_p
        d.interception_create_context.argtypes = []
        d.interception_destroy_context.restype = None
        d.interception_destroy_context.argtypes = [ctypes.c_void_p]
        d.interception_send.restype = ctypes.c_int
        d.interception_send.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.POINTER(KeyStroke), ctypes.c_uint]
        d.interception_is_keyboard.restype = ctypes.c_int
        d.interception_is_keyboard.argtypes = [ctypes.c_int]

        self._ctx = d.interception_create_context()
        if not self._ctx:
            raise InterceptionError("interception_create_context 失败（驱动是否已安装并重启？）")
        self._device = 1          # 键盘设备槽 1..10，发送失败时自动探测
        self._closed = False

    # -- 扫描码 -> 笔画 --
    @staticmethod
    def _stroke(key, up):
        _name, _vk, scan, extended = key
        state = INTERCEPTION_KEY_E0 if extended else INTERCEPTION_KEY_DOWN
        if up:
            state |= INTERCEPTION_KEY_UP
        return KeyStroke(code=scan, state=state, information=0)

    def _send(self, strokes):
        arr = (KeyStroke * len(strokes))(*strokes)
        ret = self._dll.interception_send(
            self._ctx, self._device, arr, len(strokes))
        if ret != 0:
            return
        # 当前设备槽发送失败：逐个探测可用键盘设备
        for dev in range(1, INTERCEPTION_MAX_KEYBOARD + 1):
            if dev == self._device:
                continue
            if self._dll.interception_is_keyboard(dev) and \
                    self._dll.interception_send(self._ctx, dev, arr, len(strokes)) != 0:
                self._device = dev
                return
        raise InterceptionError(
            "interception_send 全部键盘设备均失败（驱动未加载或未重启？）")

    # -- 对外接口（参数为 hotkey_forwarder 解析后的 _keys 列表） --
    def key_down(self, keys):
        self._send([self._stroke(k, up=False) for k in keys])

    def key_up(self, keys):
        self._send([self._stroke(k, up=True) for k in reversed(keys)])

    def key_click(self, keys, hold_seconds=0.06):
        self._send([self._stroke(k, up=False) for k in keys])
        time.sleep(hold_seconds)
        self._send([self._stroke(k, up=True) for k in reversed(keys)])

    def close(self):
        if not self._closed:
            self._dll.interception_destroy_context(self._ctx)
            self._closed = True


def try_create():
    """工厂：成功返回后端实例，失败返回 None（由上层回退 SendInput）。"""
    try:
        b = InterceptionBackend()
        return b
    except Exception as e:
        print(f"[key] Interception 后端不可用，回退 SendInput: {e}",
              file=sys.stderr)
        return None
