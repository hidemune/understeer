#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
understeer.py — unitedWheelShifter 汎用ツール “UnderSteer”
  - 名前に "wheel" / "shift" を含むデバイスを自動選定（または明示指定）
  - 検出デバイスの一覧表示（ベンダID/プロダクトID、phys/uniq を含む）
  - wheelshifter の ABS/BUTTON を結合した仮想コントローラを生成（/dev/uinput）
  - 物理入力イベントを仮想へ統合ルーティング

依存:
  pip install evdev

推奨 udev（例）: /etc/udev/rules.d/90-uinput.rules
  KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS="static_node=uinput"
  # ユーザを 'input' グループへ
"""

import argparse
import asyncio
import collections
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Tuple
import os
import re
import sys
from dataclasses import dataclass

from typing import Dict, List, Optional, Set, Tuple
from pathlib import Path
from itertools import chain
from evdev import InputDevice, list_devices, ecodes, categorize, UInput, AbsInfo

import fcntl
import threading

import struct
import ctypes
import traceback

import errno, struct


import time
import logging
from collections import defaultdict

import sys, logging, signal
import faulthandler; faulthandler.enable()  # 例外時・終了時にスタックを必ず出す
from evdev.ecodes import ABS

from evdev.ecodes import ABS as EC_ABS

def _resolveAbsCode(code_or_name):
    """ 'ABS_2' / 'ABS_THROTTLE' / 2 → int （失敗 None）"""
    if isinstance(code_or_name, int):
        return code_or_name
    s = str(code_or_name).strip().upper()
    if s.isdigit():
        return int(s)
    if s.startswith("ABS_") and s[4:].isdigit():
        return int(s.split("_",1)[1])
    return EC_ABS.get(s, None)

from evdev.ecodes import ecodes as EC  # 名前→コードの総合辞書

def _resolveKeyCode(code_or_name):
    """
    'KEY_A' / 'BTN_SOUTH' / 'BTN_0' / 'BTN0' / 304 / 0x130 → int
    見つからなければ None を返す。
    """
    # すでに数値ならそのまま
    if isinstance(code_or_name, int):
        return code_or_name

    # 文字列へ正規化
    s = str(code_or_name).strip().upper()
    if not s:
        return None

    # 数値表現（10進/16進）を許容
    try:
        if s.startswith("0X"):
            return int(s, 16)
        if s.isdigit():
            return int(s, 10)
    except Exception:
        pass

    # 記号ゆらぎを吸収（- を _ に、連続 _ を1つに）
    s = s.replace("-", "_")
    while "__" in s:
        s = s.replace("__", "_")

    # よくある略記の吸収: 'BTN0' → 'BTN_0'
    if s.startswith("BTN") and not s.startswith("BTN_"):
        # 例: BTN0 / BTN1 / BTN_SOUTH はそのまま
        tail = s[3:]
        if tail and tail[0].isdigit():
            s = "BTN_" + tail

    # ここまで整えた名前で evdev の総合辞書を引く
    return EC.get(s, None)

def _findReverseOption(axisMappings, srcTag: str, srcCode: int):
    """
    axisMappings から srcTag × srcCode に合致する行を探し、
    {'reverse': True/False, ...} を返す。無ければ None。
    ※ srcAbs が 'ABS_2' 文字列で入っていても吸収する。
    """
    logging.debug("_findReverseOption")
    logging.debug(srcTag)
    logging.debug(srcCode)
    target = int(srcCode)
    for row in axisMappings:
        tag = (row.get("srcTag") or "").strip().lower()
        if tag != srcTag:
            continue
        cand = int(row.get("srcAbs"))
        if cand == target:
            return row.get("options")
    return None

# SIGUSR1 を送ると全スレッドのスタックを即時ダンプできる:
def _dump_stacks(signum, frame):
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
signal.signal(getattr(signal, "SIGUSR1", signal.SIGINT), _dump_stacks)

# ログ詳細化（必要なら上書き）
logging.getLogger().setLevel(logging.DEBUG)

axisMappings = []
def invertRawValue(v: int, vmin: int, vmax: int) -> int:
    # 中心反転の一般式。双極/単極どちらでもOK。
    return vmin + vmax - v
def findAxisRow(axisMappings, srcTag: str, srcAbsCode: str | int):
    """最初に一致した1行（dict）を返す。無ければ None。"""
    #print(axisMappings)
    for row in axisMappings:
        if row.get("srcTag") == srcTag:
            if int(row.get("note")) == int(srcAbsCode):
                return row
    return None

# --- TSV を [(src_type, src_code), ...] へ正規化する共通ヘルパ ---
def _normalize_groups(groups):
    """
    groups: [
      [ ( 'ABS', 0 ), ( 'ABS', 16 ), ... ],               # 既に2タプル形式
      [ (jsi,dname,tag,stype,scode_name,scode,defv), ...] # 7列形式
    ] の混在を受け取り、すべて (src_type:str, src_code:int) に揃える
    """
    out = []
    for grp in groups:
        ng = []
        for row in grp:
            if not isinstance(row, (list, tuple)):
                continue
            if len(row) >= 2 and isinstance(row[0], str) and isinstance(row[1], (int, str)):
                # 2タプル（ or 2要素以上で先頭がstype/次がscode 的な形）
                stype = str(row[0]).strip()
                try:
                    scode = int(row[1])
                except Exception:
                    continue
                ng.append((stype, scode))
            elif len(row) >= 7:
                # 7列TSV: [js_index, device_name, src_tag, src_type, src_code_name, src_code, default_virtual, ...]
                stype = str(row[3]).strip()
                try:
                    scode = int(row[5])
                except Exception:
                    # 名前→コード救済
                    from evdev import ecodes as _EC
                    scode = _EC.ecodes.get(str(row[4]).strip(), None)
                    if scode is None:
                        continue
                ng.append((stype, int(scode)))
            # どれにも当てはまらなければ捨てる
        out.append(ng)
    return out

def parseOptionsCell(cell: str) -> dict:
    """
    最後の列のオプション文字列をパース。
    例: "REVERSE DEADZONE=200" → {"reverse": True, "deadzone": 200}
    今回はREVERSEのみ使えばOK。拡張を見越して汎用に。
    """
    opts = {"reverse": False}
    if not cell:
        return opts
    tokens = [t.strip() for t in cell.split() if t.strip()]
    for t in tokens:
        u = t.upper()
        if u in ("REVERSE", "INV", "INVERT", "INVERTED"):
            opts["reverse"] = True
        # 拡張例（将来用）:
        # elif u.startswith("DEADZONE="):
        #     try:
        #         opts["deadzone"] = int(u.split("=",1)[1])
        #     except ValueError:
        #         pass
    return opts

from evdev.ecodes import ABS as EC_ABS

def _toAbsCode(name_or_num: str):
    s = (str(name_or_num).strip()).upper()
    if s.isdigit():
        return int(s)
    if s.startswith("ABS_") and s[4:].isdigit():
        return int(s[4:])
    return EC_ABS.get(s, None)

# --- TSV loader (blank-line groups) ---
def _parse_mapping_tsv(path: str):
    groups, cur = [], []
    group_id = 0
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\r\n") + "\t\t\t\t\t"           # ← ここ
            if not line.strip():
                if cur:
                    groups.append(cur)
                    cur = []
                    group_id += 1               # ← グループを進める
                continue
            if line.lstrip().startswith("#"):
                continue

            cols = line.split("\t")
            if len(cols) < 8:                   # ← ここ
                continue
            cols = cols[:9]

            src_type = cols[3].strip().upper()                  # "ABS" or "KEY"
            src_tag  = cols[2].strip().lower()                  # 'wheel'等
            if src_tag not in ("wheel", "shift", "pad"):
                # 列ズレ検知のために早期スキップ（ログ推奨）
                logging.error(f"列ズレ検知  {path} ... 3rd col srcTag:{src_tag}")
                continue

            # 文字列名 or 数字どちらでも吸収
            src_code = _toAbsCode(cols[5] or cols[4])
            if src_code is None:
                continue

            virt_code = group_id    #_toAbsCode(cols[6])     # default_virtual
            try:
                options   = cols[8] or ""
                opts_dict = parseOptionsCell(options)
            except:
                options = ""
                opts_dict = []
            # ★ groups 用（ Type - src -virt ）
            cur.append((src_tag, int(src_code), group_id))

            # ★ axisMappings 用（数値で保持・group_id付与）
            entry = {
                "group_id": group_id,
                "device": cols[0],
                "srcTag": src_tag,              # 'wheel' 等
                "srcAbs": int(src_code),        # ★ 数値コードで保存！
                "virtAbs": virt_code,           # ★ ここも数値に（必要に応じて None 可）
                "note": cols[7],                # js_index_in_js
                "options": opts_dict,           # {'reverse': True/False}
            }
            axisMappings.append(entry)
            logging.info(entry)
    if cur:
        groups.append(cur)
    return groups


# 仮想順（未定義なら置く。既にあればスキップ）
try:
    VIRTUAL_AXES_ORDER
except NameError:
    VIRTUAL_AXES_ORDER = ["ABS_X", "ABS_Y", "ABS_Z", "ABS_RX", "ABS_RY", "ABS_RZ", "ABS_HAT0X", "ABS_HAT0Y", "ABS_THROTTLE", "ABS_RUDDER"]
try:
    VIRTUAL_BUTTONS_ORDER
except NameError:
    VIRTUAL_BUTTONS_ORDER = ["BTN_A", "BTN_B", "BTN_X", "BTN_Y", 
    "BTN_TL", "BTN_TR", "BTN_SELECT", "BTN_START", "BTN_MODE", "BTN_THUMBL", "BTN_THUMBR", 
    
    "BTN_TRIGGER", "BTN_THUMB", "BTN_THUMB2", "BTN_TOP", "BTN_TOP2", 
    
    "BTN_PINKIE", "BTN_BASE", "BTN_BASE2", "BTN_BASE3", "BTN_BASE4",
    "BTN_BASE5", "BTN_BASE6", 
    "BTN_0", "BTN_1", "BTN_2", "BTN_3", "BTN_4", "BTN_5", 
    "BTN_6", "BTN_7", "BTN_8", "BTN_9", "BTN_DEAD"]

# --- routing builder ---
def build_routing_from_tsv(axes_path: str|None, btns_path: str|None):
    """
    戻り:
      virt2src: dict[str vname] -> list[(src_type, src_code)]
      src2virt: dict[(src_type, src_code)] -> list[str vname]
      map_src2virt_abs: dict[("ABS", code)] -> int vcode
      map_src2virt_key: dict[("KEY", code)] -> int vcode
    """
    logging.info("build_routing_from_tsv {axes_path} {btns_path}")
    from evdev import ecodes as _EC
    axes_groups   = _parse_mapping_tsv(axes_path) if axes_path else []
    button_groups = _parse_mapping_tsv(btns_path) if btns_path else []

    virt2src, src2virt = {}, {}
    # 仮想→物理 / 物理→仮想（名前）
    for i, grp in enumerate(axes_groups):
        logging.info(f"axes_groups / {i} {grp}")
        if i >= len(axes_groups): break
        vname = VIRTUAL_AXES_ORDER[grp[0][2]]
        logging.info(f"vname / {vname}")
        lst=[]
        for (stype, scode, vcode) in grp:
            scode=int(scode); lst.append((stype, scode))
            src2virt.setdefault((stype, scode), []).append(vname)
        virt2src[vname]=lst
    for i, grp in enumerate(button_groups):
        logging.info(f"button_groups / {i} {grp}")
        if i >= len(VIRTUAL_BUTTONS_ORDER): break
        vname = VIRTUAL_BUTTONS_ORDER[grp[0][2]]
        logging.info(f"vname / {vname}")
        lst=[]
        for (stype, scode, vcode) in grp:
            scode=int(scode); lst.append((stype, scode))
            src2virt.setdefault((stype, scode), []).append(vname)
        virt2src[vname]=lst

    # 実 emit 用（数値仮想コード）
    map_src2virt_abs, map_src2virt_key = {}, {}
    for i, vname in enumerate(VIRTUAL_AXES_ORDER):
        if i >= len(axes_groups): break
        
        for (stype, scode, vcode) in axes_groups[i]:
            map_src2virt_abs[(stype, int(scode))]=int(vcode)
    for i, vname in enumerate(VIRTUAL_BUTTONS_ORDER):
        if i >= len(button_groups): break
        
        for (stype, scode, vcode) in button_groups[i]:
            map_src2virt_key[(stype, int(scode))]=int(vcode)

    return virt2src, src2virt, map_src2virt_abs, map_src2virt_key

# Project Cars 2 で、 4.0 〜 5.0 ms ぐらい。
# 500Hz (=  2.00 ms)
# 250Hz (=  4.00 ms)
# 125Hz (=  8.00 ms)
#  60HZ (= 16.66 ms)
LoopWait_ms     = 2     # pollはmsの整数。2〜5msで調整（FH5/PCARS2向け）
LoopWait_sec    = LoopWait_ms / 1000.0

# 5 sec
wacthdog_sec = 5.0

# For Debug Log...
# ホイールの一般的なABSコード（必要なら置き換えてください）
ABS = {
    "steer":        0x00,       # Axes 0
    "throttle":     0x05,       # Axes 5
    "brake":        0x01,       # Axes 1
    "clutch":       0x06,       # Axes 6
}

# 以下はClass内部で設定
# DEBUG_TELEMETORY = False

CREATED_UI = False

import evdev
import fcntl, struct
from evdev import ecodes, InputEvent, UInput
import time

import os, fcntl, select
import signal, threading, os
import time, threading

def set_initial_ff_gain(_fd, percent):
    """FFB初期ゲイン設定。percentは0～100"""
    # AutoCenter 設定
    ac_value = int((percent / 100.0) * 0xFFFF)
    ac_event = struct.pack("llHHI", int(time.time()), 0, ecodes.EV_FF,
                           ecodes.FF_AUTOCENTER, ac_value)
    os.write(_fd, ac_event)  # ← 修正ポイント
    print(f"Initial AutoCenter: {percent:.1f}% ({ac_value})")
    
    # Gain 設定
    gn_value = int((percent / 100.0) * 0xFFFF)
    gn_event = struct.pack("llHHI", int(time.time()), 0, ecodes.EV_FF, 
                            ecodes.FF_GAIN, gn_value)
    os.write(_fd, gn_event)  # ← 修正ポイント
    print(f"Initial Gain: {percent:.1f}% ({gn_value})")


import logging
import sys
import time
from typing import Optional


class DeltaColorFormatter(logging.Formatter):
    """
    単一フォーマッタで:
      - asctime(絶対時刻)
      - 前回ログからの経過時間 Δms（monotonicベース）
      - レベル別カラー（コンソール用）
    を同時に出す。
    """
    COLORS = {
        logging.DEBUG:    "\033[36m",  # Cyan
        logging.INFO:     "\033[32m",  # Green
        logging.WARNING:  "\033[33m",  # Yellow
        logging.ERROR:    "\033[31m",  # Red
        logging.CRITICAL: "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None, use_color: bool = True):
        if fmt is None:
            fmt = "%(asctime)s.%(msecs)03d [Δ%(delta_ms)7.1f ms] [%(levelname)s] %(message)s"
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.use_color = use_color
        self._last_mono: Optional[float] = None
        self._error_count: int = 0  # 累積エラー件数

    def format(self, record: logging.LogRecord) -> str:
        # Δ（直前ログからの経過）の計算は monotonic を使用
        now_mono = time.monotonic()
        if self._last_mono is None:
            delta_ms = 0.0
        else:
            delta_ms = (now_mono - self._last_mono) * 1000.0
        self._last_mono = now_mono

        # エラー件数カウント
        if record.levelno >= logging.ERROR:
            self._error_count += 1

        # Delta をレコードに埋め込む（フォーマット文字列で使えるように）
        record.delta_ms = delta_ms

        # 親クラスで整形
        out = super().format(record)

        # 行頭に累積エラー件数を追加
        out = f"E.Cnt:{self._error_count:02d} {out}"

        # 色付け（必要なときだけ）
        if self.use_color:
            color = self.COLORS.get(record.levelno)
            if color:
                out = f"{color}{out}{self.RESET}"
        return out


class RateLimitedLogger:
    """一定間隔 or 変化量しきい値を満たした時だけログを出す"""
    def __init__(self, min_interval_ms=5000, min_delta=200):
        self.min_interval = min_interval_ms / 1000.0
        self.min_delta = min_delta
        self._last_emit_t = 0.0
        self._last_vals = {}

    def should_emit(self, vals: dict) -> bool:
        now = time.monotonic()
        by_time = (now - self._last_emit_t) >= self.min_interval

        # 変化量で判定
        by_delta = False
        for k, v in vals.items():
            pv = self._last_vals.get(k)
            if pv is None:
                by_delta = True
                break
            if abs(int(v) - int(pv)) >= self.min_delta:
                by_delta = True
                break

        if by_time or by_delta:
            self._last_emit_t = now
            self._last_vals = vals.copy()
            return True
        return False

def setup_logger(
    level=logging.DEBUG,
    datefmt="%H:%M:%S",
    to_stderr=True,
    log_file: Optional[str] = None
):
    handlers = []

    # コンソール（stderr）: 色あり
    if to_stderr:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(DeltaColorFormatter(datefmt=datefmt, use_color=True))
        handlers.append(console)

    # ファイル出力: 色なし
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(DeltaColorFormatter(datefmt=datefmt, use_color=False))
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)


# === 使い方（テスト） ===
setup_logger(log_file=None)  # ファイルにも出したい場合はパスを渡す
print("")  # 見やすさのための空行
logging.info("Log Test Info")
time.sleep(0.2)
logging.debug("Log Test Debug")
time.sleep(0.5)
logging.warning("Log Test Warning")
time.sleep(0.7)
logging.error("Log Test Error")
print("")



# 多重起動の予防
import fcntl, os, sys
_lockf = open('/tmp/understeer.lock', 'w')
try:
    fcntl.lockf(_lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print('UnderSteer is already running. Exit.', file=sys.stderr)
    sys.exit(1)



def _sanitize_periodic(eff):
    """FF_PERIODICの最低限の正規化。EINVALを避けるための下駄"""
    p = eff.u.periodic
    # 1) magnitude==0 は多くの実機で EINVAL。→ ゼロは送らずソフト側で成功扱いにする方が安全
    if int(p.magnitude) == 0:
        return "skip_zero"

    # 2) waveform 非対応なら SINE へ
    #if int(p.waveform) not in SUPPORTED_WAVEFORMS:
    #    p.waveform = ecodes.FF_SINE

    # 3) period==0 を 1ms に
    if int(p.period) <= 0:
        p.period = 1

    # 4) 範囲ガード（Linux input の想定範囲）
    # magnitude: 0..0x7fff（符号付きだが正で使うのが通例）
    if int(p.magnitude) < 1:
        p.magnitude = 1
    if int(p.magnitude) > 0x7fff:
        p.magnitude = 0x7fff

    # offset: -0x7fff..0x7fff
    if int(p.offset) < -0x7fff:
        p.offset = -0x7fff
    if int(p.offset) >  0x7fff:
        p.offset =  0x7fff

    # phase: 0..0xffff（実装によるが 0..0x7fff でもOK）
    if int(p.phase) < 0:
        p.phase = 0

    # envelope: 長さ(ms)は >=0、level は 0..0x7fff
    env = p.envelope
    for name in ("attack_length", "fade_length"):
        if int(getattr(env, name)) < 0:
            setattr(env, name, 0)
    for name in ("attack_level", "fade_level"):
        v = int(getattr(env, name))
        if v < 0: setattr(env, name, 0)
        if v > 0x7fff: setattr(env, name, 0x7fff)

    # replay: length==0 は弾く実装あり → 最低1ms
    if int(eff.replay.length) <= 0:
        eff.replay.length = 1

    # direction 未設定なら 0
    if int(eff.direction) < 0:
        eff.direction = 0

    return "ok"

import ctypes
import logging

def dump_ctypes_struct(obj, indent=0):
    """
    任意の ctypes.Structure を再帰的に展開して内容を返す。
    """
    pad = ' ' * indent
    lines = []

    # Structure でなければそのまま返す
    if not isinstance(obj, ctypes.Structure):
        return f"{pad}{repr(obj)}"

    struct_name = obj.__class__.__name__
    lines.append(f"{pad}{struct_name}(")
    for field_name, field_type in obj._fields_:
        val = getattr(obj, field_name)
        if isinstance(val, ctypes.Structure):
            nested = dump_ctypes_struct(val, indent + 2)
            lines.append(f"{pad}  {field_name} =\n{nested}")
        elif isinstance(val, (list, tuple)):
            lines.append(f"{pad}  {field_name} = {list(val)}")
        else:
            lines.append(f"{pad}  {field_name} = {val!r}")
    lines.append(f"{pad})")
    return '\n'.join(lines)


# 先頭の ioctl 定義群の近くに統一して記載（重複定義は削除）
EVIOCSFF  = 0x40304580  # in/out
EVIOCRMFF = 0x40044581  # int 引数 (_IOW('E', 0x81, int))

# 先頭の import 付近
import struct
from collections.abc import Mapping

# --- add: OR 合流用のボタン・コアレッサ ---
from collections import defaultdict

# --- add: HAT 合流（priority / last） ---
import time
from collections import defaultdict

try:
    from evdev import ecodes as _EC
    _HATX = getattr(_EC, "ABS_HAT0X", 16)
    _HATY = getattr(_EC, "ABS_HAT0Y", 17)
except Exception:
    _HATX, _HATY = 16, 17  # フォールバック

class _HatCoalesce:
    """
    HAT(ABS_HAT0X / ABS_HAT0Y)を複数ソースから1つの仮想へ合流。
    mode='priority' : 先に定義/遭遇したソースを優先（最初に非0を出しているソースを採用）
    mode='last'     : 最後に変化したソースを採用
    使い方（どれでもOKな柔軟API）:
      update(vcode, value)
      update(vcode, src, value)
      update(src, vcode, value)   # src と vcode の順が逆でも判定して補正
    エミッタ emit は emit(abs_code:int, value:int) を想定（-1/0/1）
    """
    def __init__(self, emit, mapping_virt2src=None, mode="priority"):
        self.emit = emit
        self.mode = "last" if mode == "last" else "priority"
        self._vals = defaultdict(dict)        # vcode -> {src_key: value(-1/0/1)}
        self._ts   = defaultdict(dict)        # vcode -> {src_key: last_change_ts}
        self._order= defaultdict(list)        # vcode -> [src_key,...]（priority用既定順）
        self._last_out = {}                   # vcode -> last emitted value

        # 可能ならマッピングから優先順を初期化（形は不問、文字列化してキー化）
        if mapping_virt2src:
            for vcode, src_list in mapping_virt2src.items():
                for s in src_list:
                    sk = self._src_key(s)
                    if sk not in self._order[vcode]:
                        self._order[vcode].append(sk)

    def _src_key(self, obj):
        # src を一意化するためのキー化（タプル/辞書/数値/文字列どれでもOKに）
        try:
            if isinstance(obj, (list, tuple)):
                return "t:" + "|".join(map(str, obj))
            if isinstance(obj, dict):
                return "d:" + "|".join(f"{k}={v}" for k,v in sorted(obj.items()))
            return "s:" + str(obj)
        except Exception:
            return "s:" + repr(obj)

    # 既存の _parse_args を以下に置き換え
    def _parse_args(self, *args):
        """
        許容:
          (vcode, value)
          (vcode, src, value)
          (src, vcode, value)
          (vname, vcode, src_tag, src_code, value)  # ← 今回これ
        戻り: (vcode:int, src_key:str|None, value:int)
        """
        if len(args) < 2:
            raise TypeError("update() expects at least 2 arguments")

        # value は“最後の int”とみなす（-1/0/1で丸め）
        ints = [a for a in args if isinstance(a, int)]
        if not ints:
            value = 0
        else:
            value = ints[-1]
        try:
            value = int(value)
            value = -1 if value < 0 else (1 if value > 0 else 0)
        except Exception:
            value = 0

        # vcode は HAT のコード(16/17)を優先的に拾う。無ければ最初の int を採用
        vcode = None
        for a in args:
            if isinstance(a, int) and a in (_HATX, _HATY):
                vcode = a
                break
        if vcode is None:
            # 最初の int にフォールバック
            vcode = next((a for a in args if isinstance(a, int)), _HATX)

        # src は “vcode/value 以外”をまとめてキー化
        src_parts = []
        for a in args:
            if a is vcode or (isinstance(a, int) and a == value):
                continue
            src_parts.append(a)
        src_key = None if not src_parts else self._src_key(tuple(src_parts))

        return int(vcode), src_key, int(value)

    # これをクラスの末尾あたりに追加（エイリアス）
    def on(self, *args):
        self.update(*args)

    def update(self, *args):
        vcode, src_key, value = self._parse_args(*args)

        # HAT 以外は素通し
        if vcode not in (_HATX, _HATY):
            if self._last_out.get(vcode) != value:
                self._last_out[vcode] = value
                self.emit(vcode, value)
            return

        now = time.monotonic()

        # src_key が無い（単独ソース）の場合は即時反映
        if src_key is None:
            if self._last_out.get(vcode) != value:
                self._last_out[vcode] = value
                self.emit(vcode, value)
            return

        # 値・時刻を記録（遭遇順を保持）
        self._vals[vcode][src_key] = value
        self._ts[vcode][src_key] = now
        if src_key not in self._order[vcode]:
            self._order[vcode].append(src_key)

        # 合流ポリシで代表値を決定
        if self.mode == "last":
            # 直近で変化した非0を優先、全0なら0
            cand = [(sk, self._ts[vcode].get(sk, 0), self._vals[vcode].get(sk, 0))
                    for sk in self._vals[vcode]]
            # 非0のみ
            nonzero = [t for t in cand if t[2] != 0]
            if nonzero:
                nonzero.sort(key=lambda t: t[1], reverse=True)
                out = nonzero[0][2]
            else:
                out = 0
        else:
            # priority: _order の先頭から見て最初に非0の値を採用。全0なら0
            out = 0
            for sk in self._order[vcode]:
                v = self._vals[vcode].get(sk, 0)
                if v != 0:
                    out = v
                    break

        # 変化した時だけ emit
        if self._last_out.get(vcode) != out:
            self._last_out[vcode] = out
            self.emit(vcode, out)

    # 互換用エイリアス
    set = update
    put = update
    on_event = update

class _ButtonCoalesce:
    """
    複数の物理ボタンを1つの仮想ボタンに OR 合流するための小物。
    press/release で参照カウント、0→1/1→0時のみ emit する。
    """
    def __init__(self, emit):
        self.emit = emit                    # emit(key_code, value[0/1])
        self._ref = defaultdict(int)        # key_code -> active press count

    def press(self, code: int):
        c = self._ref[code]
        if c == 0:
            self.emit(code, 1)              # 立上りのみ送出
        self._ref[code] = c + 1

    def release(self, code: int):
        c = self._ref.get(code, 0)
        if c <= 1:
            self._ref[code] = 0
            self.emit(code, 0)              # 立下りのみ送出
        else:
            self._ref[code] = c - 1

    def update(self, code: int, is_down: bool):
        (self.press if is_down else self.release)(int(code))

    # 互換エイリアス
    on  = update
    off = lambda self, code, *_: self.update(int(code), False)


# どこか共有ユーティリティに
def _coerce_effect_id(p):
    """
    payload から effect_id(int) を取り出す。
    受ける型: dict-like / uinput_ff_erase / int / (old tuple?)
    """
    # dict / Mapping なら "effect_id"
    if isinstance(p, Mapping):
        return int(p.get("effect_id"))

    # ctypes 構造体（uinput_ff_erase 等）なら .effect_id を見る
    if hasattr(p, "effect_id"):
        return int(getattr(p, "effect_id"))

    # すでに int の場合
    if isinstance(p, int):
        return int(p)

    # 旧形式の (effect_id, ...) タプルにゆるく対応
    if isinstance(p, (tuple, list)) and p:
        try:
            return int(p[0])
        except Exception:
            pass

    return None





FF_OP_UPLOAD = "upload"
FF_OP_ERASE  = "erase"
FF_OP_PLAY   = "play"
FF_OP_STOP   = "stop"
FF_WARN_SLOW_MS = 25  # これを超えたら警告ログ



def _rmff(fd: int, eff_id: int) -> bool:
    """Return True if erased or already gone; False if still present.
       Raise only on想定外エラー。"""
    try:
        fcntl.ioctl(fd, EVIOCRMFF, int(eff_id), False)  # 重要: 第3引数はint
        return True
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOENT):
            # そのIDは既に存在しない。無害。マップ掃除だけすればOK。
            logging.debug("[FFB] EVIOCRMFF benign miss id=%d: %s", eff_id, e)
            return True
        logging.warning("[FFB] EVIOCRMFF failed id=%d: %s", eff_id, e)
        return False


def get_path_from_fd(fd: int) -> str:
    """
    指定されたファイルディスクリプタ(fd)からフルパスを取得する。
    fd が無効な場合は None を返す。
    """
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except FileNotFoundError:
        return None
    except OSError as e:
        logging.error(f"Error reading fd {fd}: {e}")
        return None



# === ここを understeer.py の適切な位置（import郡の下あたり）に追加 ===
import threading, queue, errno

class UploadTask:
    __slots__ = ("virt_id", "effect", "result_errno", "phys_id_out")
    def __init__(self, virt_id:int, effect):
        self.virt_id = virt_id
        self.effect = effect
        self.result_errno = 0
        self.phys_id_out = -1

class HidrawHandle:
    """
    hidraw と event の対応が “取れた時だけ” 使う汎用ハンドル。
    - FFB の ioctl / write は event デバイスに対して行う（hidraw は情報/制御用）。
    - ここで hidraw/event ともに O_RDWR|O_NONBLOCK で開く。
    
    使い方
    # 1) event のパスが分かっている場合（推奨）
    handle = HidrawHandle(hidraw_path=hid_path, event_path=event_path)
    # 2) 既に InputDevice を持っている場合
    handle = HidrawHandle(hidraw_path=hid_path, event_dev=event_input_device)
    
    """
    def __init__(self,
                 hidraw_path: str,
                 event_path: Optional[str] = None,
                 event_dev: Optional[InputDevice] = None):
        import os
        self.hidraw_path = hidraw_path
        # ★ hidraw は常に書き込み可能でオープン
        self.hid_fd = os.open(hidraw_path, os.O_RDWR | os.O_NONBLOCK)

        self.event_path: Optional[str] = None
        self.ui_event_fd: Optional[int] = None
        self.dev: Optional[InputDevice] = None

        if event_dev is not None:
            # 既存の InputDevice を優先的に使う
            self.dev = event_dev
            try:
                self.event_path = event_dev.path
            except Exception:
                self.event_path = event_path
        else:
            self.event_path = event_path
            if self.event_path:
                # 読み書き可能で開ける
                self.dev = InputDevice(self.event_path)

        # ★ event 側の“書込FD”を必ず確保（InputDevice が RO の場合でも確実に書けるようにする）
        if self.event_path:
            try:
                self.ui_event_fd = os.open(self.event_path, os.O_RDWR | os.O_NONBLOCK)
            except PermissionError:
                # 失敗時は None。FFB は使えないが dev.write() が使える環境もあるので保持
                logging.error("FFB は使えないが dev.write() が使える環境もあるので保持")
                self.ui_event_fd = None

    def close(self):
        """明示的クローズ（必要に応じて呼び出し側で管理）"""
        import os
        if getattr(self, "dev", None):
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        if getattr(self, "event_fd", None) is not None:
            try:
                os.close(self.ui_event_fd)
            except Exception:
                pass
            self.ui_event_fd = None
        if getattr(self, "hid_fd", None) is not None:
            try:
                os.close(self.hid_fd)
            except Exception:
                pass
            self.hid_fd = None


import errno, logging, time, select, fcntl, os
from evdev import ecodes

def write_ff_safe(dev, code, value, *,
                  timeout=0.25,           # 1回の書き込み許容待ち時間
                  retries=3,              # リトライ回数
                  backoff=0.02,           # EAGAIN時の間隔
                  poll_before=False):      # POLLOUTを待ってから書く T / その他 F
    """
    EV_FF 書き込み用の堅牢ラッパ。
    - デバイスビジー時(EAGAIN/EWOULDBLOCK)に短いバックオフで再試行
    - select.poll(POLLOUT)でfdの出力準備を待つ（キャラデバイスでも有効なことが多い）
    - 長時間ブロックしそうならタイムアウトで諦めて上位へ返す
    戻り値: "ok" / "skip" / "timeout" / "gone" / "unsupported" / "ioerr"
    """
    logging.error("ここって使ってる？？")
    try:
        fd = dev.fileno()
        # 必須ではないが、念のため fd 実体をログできるように
        # logging.debug("FF fd -> %s", os.readlink(f"/proc/self/fd/{fd}"))
    except Exception:
        return "gone"
    """
    # 事前に POLLOUT 待ち（任意）
    if poll_before:
        p = select.poll()
        p.register(fd, select.POLLOUT | select.POLLERR | select.POLLHUP)
        events = p.poll(int(timeout * 1000))
        if not events:
            return "timeout"
        # エラー/切断チェック
        for _, ev in events:
            if ev & (select.POLLERR | select.POLLHUP):
                return "gone"
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            # EV_FF 書き込み用の堅牢ラッパ。
            dev.write(ecodes.EV_FF, code, int(value))
            dev.syn()
            return "ok"
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                if time.monotonic() >= deadline:
                    return "timeout"
                time.sleep(backoff)
                if attempt <= retries:
                    continue
                return "timeout"
            elif e.errno in (errno.ENODEV, errno.ENXIO):   # デバイス消滅/切断
                return "gone"
            elif e.errno == errno.EINVAL:                  # 未対応 or 引数不正
                return "unsupported"
            elif e.errno == errno.EIO:
                return "ioerr"
            else:
                logging.exception("EV_FF write unexpected errno=%s", e.errno)
                return f"errno:{e.errno}"
        except Exception:
            logging.exception("EV_FF write unexpected error")
            return "ioerr"



class AsyncFFBProxy:
    """ 物理機器への FFB 送信 """
    def __init__(self, open_physical_ff_target, ff_mapper, us):
        """open_physical_ff_target: () -> HidrawHandle"""
        self._open_target = open_physical_ff_target  # 関数（まだ呼ばない）

        self.ff_mapper = ff_mapper
        self.ui = us

        #self._queue: asyncio.Queue[_QueuedOp] = asyncio.Queue(maxsize=1024)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._handle: Optional[HidrawHandle] = None
        # ここではデバイスに触れない（遅延オープン）
        logging.debug("AsyncFFBProxy: init (lazy target opener)")



    def _phys_fd(self) -> Optional[int]:
        """物理 /dev/input/eventX の fd を返す"""
        if not self._handle:
            return None
        if getattr(self._handle, "event_fd", None) is not None:
            return self._handle.ui_event_fd
        dev = getattr(self._handle, "dev", None)
        return getattr(dev, "fd", None)

    def _upload_from_payload(self, kind: str, payload: dict) -> tuple[str, Optional[int]]:
        """
        enqueue された upload_* payload を EVIOCSFF で物理へアップロードし、
        物理 effect_id を返す。
        戻り値: (status, phys_id or None)
        """
        logging.error("使ってないはず")
        fd = self._phys_fd()
        dev = getattr(self._handle, "dev", None)
        if fd is None or dev is None:
            return ("no-handle", None)

        eff = payload.get("effect", {})
        virt_id = int(eff.get("id", -1))
        replay  = eff.get("replay", {})
        length  = int(replay.get("length", 0))
        delay   = int(replay.get("delay", 0))

        # build_* に渡すフォーマットへ整形
        params = {"length_ms": length, "delay_ms": delay}

        from evdev import ecodes as E

        if kind == "upload_constant":
            const = eff.get("constant", {})
            params.update({"level": int(const.get("level", 0))})
            buf = build_effect_constant({"effect_id": -1, "params": params})

        elif kind == "upload_spring":
            s = eff.get("spring", {})
            params.update({
                "right_saturation": int(s.get("right_sat", 0x7FFF)),
                "left_saturation":  int(s.get("left_sat",  0x7FFF)),
                "right_coeff":      int(s.get("right_coef", -5000)),
                "left_coeff":       int(s.get("left_coef",  -5000)),
                "deadband":         int(s.get("deadband", 0)),
                "center":           int(s.get("center", 0)),
            })
            buf = build_effect_condition({"effect_id": -1, "params": params}, E.FF_SPRING)

        elif kind == "upload_damper":
            d = eff.get("damper", {})
            params.update({
                "right_saturation": int(d.get("right_sat", 0x7FFF)),
                "left_saturation":  int(d.get("left_sat",  0x7FFF)),
                "right_coeff":      int(d.get("right_coef", -5000)),
                "left_coeff":       int(d.get("left_coef",  -5000)),
                "deadband":         int(d.get("deadband", 0)),
                "center":           int(d.get("center", 0)),
            })
            buf = build_effect_condition({"effect_id": -1, "params": params}, E.FF_DAMPER)

        elif kind == "upload_rumble":
            r = eff.get("rumble", {})
            params.update({
                "strong_magnitude": int(r.get("strong_magnitude", 0)),
                "weak_magnitude":   int(r.get("weak_magnitude", 0)),
            })
            buf = build_effect_rumble({"effect_id": -1, "params": params})

        else:
            return ("unsupported-kind", None)

        try:
            phys_id = upload_effect(fd, buf)  # EVIOCSFF in/out
            self.ff_mapper._virt2phys[int(virt_id)] = int(phys_id)
            logging.debug("Pys / upload/EVIOCSFF %s: virt_id=%s -> phys_id=%s",
                          kind, virt_id, phys_id)
            return ("ok", phys_id)
        except Exception as e:
            clear_ff_effects(dev)
            return ("err-upload", None)

    def _map_effect_id(self, vid: int) -> int:
        """仮想IDを物理IDへ。未登録なら元の値を返す（保険）"""
        try:
            return int(self.ff_mapper._virt2phys.get(int(vid), int(vid)))
        except Exception:
            return int(vid)


    def _dispatch_blocking(self, op):
        """
        物理ホイール(dev = evdev.InputDevice想定)へブロッキングで指示を送る。
        kind:
          - gain:        {"value": 0..65535 or 0..100}
          - autocenter:  {"value"| "enabled"| "autocenter"}
          - play:        {"effect_id": int, "value": int=1}  # EV_FF: start(1)/repeat(n)
          - stop:        {"effect_id": int}                   # EV_FF: stop(0)
          - erase:       {"effect_id": int}
          - upload|update: 将来実装（現状はスタブ）
        """
        logging.error("使ってないはず")
        path = getattr(getattr(self._handle, "dev", None), "path", "(no-handle)")
        logging.debug(f"[FFB-Pys-SendToWheel]<dispatch_block>: {op} {path}")
        # op は (kind, payload) か、_QueuedOp(kind=..., payload=...) のどちらか
        try:
            # dataclass / namedtuple 風
            kind = op.kind
            p = op.payload
        except AttributeError:
            # 旧形式のタプル風
            kind, p = op
        dev  = getattr(self._handle, "dev", None)  # evdev.InputDevice を想定

        if dev is None:
            logging.error("_dispatch_blocking: ERR (no-handle)")
            return "no-handle"

        # 1) 
        if kind == "gain":
            v = int(p.get("value", 0))
            if   v < 0:     v = 0
            elif v > 65535: v = 65535

            # 物理デバイスへ反映
            #dev.write(ecodes.EV_FF, ecodes.FF_GAIN, v)
            #dev.syn()
            ret = write_ff_safe(dev, ecodes.FF_GAIN, v)
            logging.debug("物理デバイスへ反映 ok:gain / %s", ret)
            return "ok:gain"
        if kind == "autocenter":
            # 受け口は柔軟に：enabled / value / autocenter のいずれか
            v = p.get("value", None)
            if v is None:
                if "enabled" in p:
                    v = 65535 if bool(p["enabled"]) else 0
                elif "autocenter" in p:
                    v = int(p["autocenter"])
                else:
                    v = 0  # デフォルトでOFF
            # 0..100 を送ってくるゲーム対策：0..65535 へ正規化
            try:
                v = int(v)
            except Exception:
                v = 0
            if 0 <= v <= 100:
                v = int(round(v * 655.35))
            # クランプ
            if v < 0:
                v = 0
            elif v > 65535:
                v = 65535
            # 物理デバイスへ反映
            ret = write_ff_safe(dev, ecodes.FF_AUTOCENTER, v)
            logging.debug("物理デバイスへ反映 ok:autocenter / %s", ret)
            return "ok:autocenter"

        if self.us.ff_passthrough_easy:
            return "skip:ff_passthrough_easy-only"

        # 2) 再生/停止（Linux evdevの標準：EV_FF, code=effect_id, value=1/0）
        if kind == "play":
            eff_id = p.get("effect_id")
            if eff_id is None:
                logging.warning("[FFB-Pys-SendToWheel] play missing effect_id -> ignored")
                return "skip:play-missing-effect-id"
            try:
                eff_id = int(eff_id)
            except Exception:
                logging.warning("[FFB-Pys-SendToWheel] play bad effect_id -> ignored")
                return "skip:play-bad-effect-id"

            # repeat 既定値は 1（ゲーム次第で >1 が来ることもある）
            repeat = p.get("value", 1)
            try:
                repeat = int(repeat)
            except Exception:
                repeat = 1
            repeat = max(1, repeat)  # 0 は stop と紛らわしいので 1 以上に丸める

            ret = write_ff_safe(dev, eff_id, repeat)  # EV_FF/code=eff_id/value=repeat
            logging.debug("[FFB-Pys-SendToWheel] ok:play id=%d repeat=%d / %s", eff_id, repeat, ret)
            return "ok:play"

        if kind == "stop":
            eff_id = p.get("effect_id")
            if eff_id is None:
                logging.warning("[FFB-Pys-SendToWheel] stop missing effect_id -> ignored")
                return "skip:stop-missing-effect-id"
            try:
                eff_id = int(eff_id)
            except Exception:
                logging.warning("[FFB-Pys-SendToWheel] stop bad effect_id -> ignored")
                return "skip:stop-bad-effect-id"

            # stop は value=0 を送る
            ret = write_ff_safe(dev, eff_id, 0)
            logging.debug("[FFB-Pys-SendToWheel] ok:stop id=%d / %s", eff_id, ret)
            return "ok:stop"

        # 3) アップロード／更新（仮想→物理 変換と実エフェクト生成）
        if kind in ("upload_constant", "upload_spring", "upload_damper", "upload_rumble"):
            st, phys_id = self._upload_from_payload(kind, p)
            return f"{st}:{kind}"

        if kind in ("upload", "update"):
            # 旧一般名（使わない想定だが保険）
            et = p.get("effect_type")
            logging.debug("[FFB-Pys-SendToWheel] %s-stub (effect_type=%s)", kind, et)
            return f"ok:{kind}-stub"

        # 4) 再生/停止（タイプ別の play_* を優先）
        if kind in ("play_constant", "play_spring", "play_damper", "play_rumble"):
            eff_id = p.get("effect_id")
            if eff_id is None:
                return f"skip:{kind}-missing-id"
            peid = self._map_effect_id(eff_id)
            repeat = int(p.get("value", 1))
            ret = write_ff_safe(dev, peid, max(1, repeat))
            logging.debug("Pys / %s virt_id=%s -> phys_id=%s / %s",
                          kind, eff_id, peid, ret)
            return f"ok:{kind}"

        if kind == "play":
            eff_id = p.get("effect_id")
            if eff_id is None:
                return "skip:play-missing-id"
            peid = self._map_effect_id(eff_id)
            repeat = int(p.get("repeat", p.get("value", 1)))
            ret = write_ff_safe(dev, peid, max(1, int(repeat)))
            logging.debug("Pys / play virt_id=%s -> phys_id=%s / %s",
                          eff_id, peid, ret)
            return "ok:play"

        if kind == "stop":
            eff_id = p.get("effect_id")
            if eff_id is None:
                return "skip:stop-missing-id"
            peid = self._map_effect_id(eff_id)
            ret = write_ff_safe(dev, peid, 0)
            logging.debug("Pys / stop virt_id=%s -> phys_id=%s / %s",
                          eff_id, peid, ret)
            return "ok:stop"

        if kind in ("erase", "remove", "upload_erase", "eviocrmff"):
            eff_id = _coerce_effect_id(p)
            if eff_id is None:
                logging.error("Pys / erase: payload has no effect_id: %r", p)
                return "bad-payload"
            # ★ 必ず O_RDWR の eventFD を使う Pys / EVIOCRMFF
            fd = self._phys_fd()
            if fd is None:
                logging.error("Pys / EVIOCRMFF failed: no phys fd")
                return "no-handle"
            # ★ 重要：EVIOCRMFF は int のポインタ渡し
            EVIOCRMFF = 0x40044581
            try:
                # 0 は合法な effect_id。未登録は None 判定のみ。
                phys_id = self.ff_mapper._virt2phys.get(eff_id, None)
                target_id = phys_id if phys_id is not None else int(eff_id)
                # 機器と会話
                # ★ ここが重要：int をそのまま渡す（pack しない）
                fcntl.ioctl(fd, EVIOCRMFF, int(target_id), False)
                logging.debug("Pys / EVIOCRMFF ok (id=%d)", phys_id)
                #return "ok:erase"
            except OSError as e:
                logging.error("Pys / EVIOCRMFF failed id=%d: %s", phys_id, e)
                traceback.print_exc()
                #return f"err:{e.errno}"

            # マップ掃除（同じ仮想IDが再利用される可能性もあるため、一応消す）
            try:
                self.ff_mapper._virt2phys.pop(int(eff_id), None)
            except Exception:
                pass
            return "ok:erase"

        logging.error(f"[ff] 実装が必要_dispatch_blocking skip:unknown-kind:{kind}")
        return f"skip:unknown-kind:{kind}"



"""
実装：FFB
"""
import os, fcntl, struct
from evdev import ecodes as E


def _unpack_ff_effect_id_from_buf(buf):
    # ff_effect の先頭: type(u16), id(s16), ... なので 2バイト後が s16 id
    type_u16, id_s16 = struct.unpack_from("Hh", buf, 0)
    return id_s16

def _ioctl_with_timeout(fd: int, req: int, buf, timeout_sec=2.5):
    """ioctl を別スレッドで実行してタイムアウト監視。ハングなら例外投げる。"""
    res = {"err": None}
    def _run():
        try:
            fcntl.ioctl(fd, req, buf, True)  # mutate=True
        except BaseException as e:
            res["err"] = e

    t = threading.Thread(target=_run, daemon=True, name="EVIOCSFF-call")
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        raise TimeoutError(f"ioctl stuck: req=0x{req:08x} fd={fd} path={fd_path(fd)}")
    if res["err"] is not None:
        raise res["err"]

def fd_path(fd: int) -> str:
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except Exception:
        return f"(fd={fd})"

class FfEvioMapper:
    def __init__(self):
        # 仮想id <-> 物理id の相互マップ
        self._map_lock = threading.RLock()     # ★ 追加: 再入可能ロック
        self._virt2phys: dict[int, int] = {}
        self._phys2virt: dict[int, int] = {}
        self._phys_last_used: dict[int, float] = {}  # pID -> last used (monotonic)
        self.phys_fd = None                    # ★ 後でセットされる想定
        pass

    # === FfEvioMapper 相当のクラス内に、無ければ追加 ===
    def extract_id_from_ff_effect_buf(self, buf: bytearray) -> int:
        """
        EVIOCSFF 後の ff_effect バッファから id を抽出して返す。
        ctypes.Structure(ff_effect) がある前提。
        """
        eff = ff_effect.from_buffer_copy(bytes(buf))
        return int(eff.id)

    def virt_to_phys_id_or(self, virt_id: int, default: int = -1) -> int:
        """
        virt_id -> phys_id のマップを持っている場合はそれを返す。
        無ければ default を返す。
        """
        try:
            return int(self._virt2phys.get(virt_id, default))
        except Exception:
            return default

    # 登録（UPLOAD 成功後に使う）
    def remember(self, virt_id: int, phys_id: int) -> None:
        self._virt2phys[virt_id] = phys_id
        self._phys2virt[phys_id] = virt_id

    # 参照（ERASE 時に使う）
    def phys_of(self, virt_id: int) -> int | None:
        return self._virt2phys.get(virt_id)

    # 片方の id が無効になったときの掃除
    def forget_by_virt(self, virt_id: int) -> None:
        phys = self._virt2phys.pop(virt_id, None)
        if phys is not None:
            self._phys2virt.pop(phys, None)

    def forget_by_phys(self, phys_id: int) -> None:
        virt = self._phys2virt.pop(phys_id, None)
        if virt is not None:
            self._virt2phys.pop(virt, None)

    def __repr__(self) -> str:
        logging.error("FfEvioMapper __repr__　使ってないと思う")
        # 追加したらここに出す（将来 hidraw/TMFF2 直送などの分岐名も）
        return "<FfEvioMapper backend='EVIOCSFF/EVIOCRMFF' features='upload,erase'>"

    @staticmethod
    def _ff_type_name(t: int) -> str:
        # FfEvioMapper._ff_type_name()
        m = {
            E.FF_CONSTANT: f"CONSTANT({E.FF_CONSTANT})",
            E.FF_SPRING:   f"SPRING({E.FF_SPRING})",
            E.FF_DAMPER:   f"DAMPER({E.FF_DAMPER})",
            E.FF_RUMBLE:   f"RUMBLE({E.FF_RUMBLE})",
            E.FF_PERIODIC: f"PERIODIC({E.FF_PERIODIC})",
            getattr(E, "FF_SINE", 0): f"SINE({getattr(E, 'FF_SINE', 0)})",
            getattr(E, "FF_SQUARE", 0): f"SQUARE({getattr(E, 'FF_SQUARE', 0)})",
            getattr(E, "FF_TRIANGLE", 0): f"TRIANGLE({getattr(E, 'FF_TRIANGLE', 0)})",
            getattr(E, "FF_RAMP", 0): f"RAMP({getattr(E, 'FF_RAMP', 0)})",
            
            E.FF_SAW_UP:          f"SAW_UP({E.FF_SAW_UP})",
            E.FF_SAW_DOWN:          f"SAW_DOWN({E.FF_SAW_DOWN})",
            E.FF_INERTIA:          f"INERTIA({E.FF_INERTIA})",
            E.FF_FRICTION:          f"FRICTION({E.FF_FRICTION})",
            E.FF_CUSTOM:          f"CUSTOM({E.FF_CUSTOM})",
            
            E.FF_GAIN:          f"GAIN({E.FF_GAIN})",
            E.FF_AUTOCENTER:    f"AUTOCENTER({E.FF_AUTOCENTER})",
        }
        return m.get(int(t), f"TYPE_{int(t)}")

    @staticmethod
    def _ff_effect_to_dict(eff) -> dict:
        #logging.error("_ff_effect_to_dict")
        # 共通ヘッダ
        d = {
            "type": int(eff.type),
            "type_name": FfEvioMapper._ff_type_name(eff.type),
            "id_in": int(eff.id),
            "direction": int(eff.direction),
            "trigger.button": int(eff.trigger.button),
            "trigger.interval": int(eff.trigger.interval),
            "replay.length": int(eff.replay.length),
            "replay.delay": int(eff.replay.delay),
        }
        t = int(eff.type)
        try:
            if t == E.FF_CONSTANT:
                d.update({
                    "constant.level": int(eff.u.constant.level),
                    "env.attack_len": int(eff.u.constant.envelope.attack_length),
                    "env.attack_lvl": int(eff.u.constant.envelope.attack_level),
                    "env.fade_len":   int(eff.u.constant.envelope.fade_length),
                    "env.fade_lvl":   int(eff.u.constant.envelope.fade_level),
                })
            elif t == E.FF_PERIODIC:
                d.update({
                    "periodic.waveform": int(eff.u.periodic.waveform),
                    "periodic.period":   int(eff.u.periodic.period),
                    "periodic.magnitude":int(eff.u.periodic.magnitude),
                    "periodic.offset":   int(eff.u.periodic.offset),
                    "periodic.phase":    int(eff.u.periodic.phase),
                    "env.attack_len":    int(eff.u.periodic.envelope.attack_length),
                    "env.attack_lvl":    int(eff.u.periodic.envelope.attack_level),
                    "env.fade_len":      int(eff.u.periodic.envelope.fade_length),
                    "env.fade_lvl":      int(eff.u.periodic.envelope.fade_level),
                })
            elif t in (E.FF_SPRING, E.FF_DAMPER):
                # condition[0], condition[1]
                for i in (0, 1):
                    c = eff.u.condition[i]
                    d.update({
                        f"cond{i}.right_sat": int(c.right_saturation),
                        f"cond{i}.left_sat":  int(c.left_saturation),
                        f"cond{i}.right_coef":int(c.right_coeff),
                        f"cond{i}.left_coef": int(c.left_coeff),
                        f"cond{i}.deadband":  int(c.deadband),
                        f"cond{i}.center":    int(c.center),
                    })
            elif t == E.FF_RUMBLE:
                d.update({
                    "rumble.strong": int(eff.u.rumble.strong_magnitude),
                    "rumble.weak":   int(eff.u.rumble.weak_magnitude),
                })
        except Exception:
            # フィールド未定義等で失敗しても落とさない
            pass
        return d


    def upload_ff_effect_via_eviocsff(self, phys_fd: int, ff_effect_struct) -> int:
        # 可視化に役立つログ（どの event に投げているか）
        psyfdpath = fd_path(phys_fd)
        # 書いてみる
        fcntl.ioctl(phys_fd, EVIOCSFF, ff_effect_struct, True)
        logging.info(f"Pys up OK. type={FfEvioMapper._ff_type_name(ff_effect_struct.type)}")
        
        # カーネルが書き戻した id を取り出して元構造体へ反映
        try:
            id_offset = type(ff_effect_struct).id.offset
            eff_id = ctypes.c_int16.from_buffer(ff_effect_struct, id_offset).value  # ✓ 2byte read
        except Exception as e:
            logging.error("Pys-FFB up/ ERROR")
            traceback.print_exc()
            # 最低でも id だけ返せればOK
            eff_id = -1

        if eff_id < 0:
            logging.error(f"EVIOCSFF returned invalid id={eff_id}")
            raise OSError(errno.EINVAL, f"EVIOCSFF returned invalid id={eff_id}")
        
        logging.debug(f"[Pys up] after EVIOCSFF id={eff_id}")
        return int(eff_id)

    def erase_ff_effect_via_eviocrmff(self, phys_fd: int, effect_id: int) -> None:
        psyfdpath = fd_path(phys_fd)
        fcntl.ioctl(phys_fd, EVIOCRMFF, int(effect_id), True)
        logging.info(f"Pys er OK. id={effect_id}")


# ---- input_event(カーネルに投げる再生/停止トリガ) ----
_INPUT_EVENT_FMT = "qqHHI"  # sec,usec,type,code,value（timeは0で投げてOK）
def _ie(type_, code, value):
    return struct.pack(_INPUT_EVENT_FMT, 0, 0, type_, code, value)
def pack_ie(t, c, v):  # timevalは0でOK
    return struct.pack(INPUT_EVENT_FMT, 0, 0, t, c, v)

# 共通ヘッダ(type,id,direction, trigger_button, trigger_interval, replay_length, replay_delay)
_FF_HDR_FMT = "HhH H H H H"  # = 2+2+2 +2+2 +2+2 = 14 bytes

# envelope（ここでは省略→全部0で）
# 省略時の定数（attack_length, attack_level, fade_length, fade_level）
_ENV_FMT = "HHHH"  # 8 bytes
_ENV_ZERO = struct.pack(_ENV_FMT, 0, 0, 0, 0)

# ------ Constant ------
# level(s16), envelope(8B)
_CONST_FMT = "h"  # level のみ（後にENVを連結）

# ------ Periodic ------
# waveform(u16) period(u16) magnitude(s16) offset(s16) phase(u16) envelope(8B) custom_len(u32)
_PER_FMT = "HHhhH"  # + ENV(8) + I(4)

# ------ Condition (Spring/Damper) ------
# right_sat(u16) left_sat(u16) right_coeff(s16) left_coeff(s16) deadband(u16) center(s16)
_COND_FMT = "HHhhHh"

# ------ Rumble ------
# strong(u16) weak(u16)
_RUM_FMT = "HH"


def _hdr(effect_type, eff_id, direction_deg, length_ms, delay_ms, trigger_button=0, trigger_interval=0):
    # direction: 0..360 deg -> 0..0xFFFF (カーネル仕様)
    direction = int((direction_deg % 360) * 0xFFFF / 360) if direction_deg is not None else 0
    return struct.pack(
        _FF_HDR_FMT,
        effect_type,
        int(eff_id),         # -1 で新規アップロード
        direction,
        int(trigger_button),
        int(trigger_interval),
        int(length_ms),
        int(delay_ms),
    )

# ---- 各効果ビルダー（payload["params"]から構築） ----

def build_effect_constant(payload):
    p = payload["params"]
    level = int(max(-32768, min(32767, p.get("level", 0))))
    length = int(p.get("length_ms", 0))
    delay  = int(p.get("delay_ms", 0))
    direction_deg = int(p.get("direction_deg", 0))
    eff_id = int(payload.get("effect_id", -1))

    buf = bytearray()
    buf += _hdr(E.FF_CONSTANT, eff_id, direction_deg, length, delay)
    buf += struct.pack(_CONST_FMT, level)
    buf += _ENV_ZERO
    return buf

def build_effect_periodic_sine(payload):
    p = payload["params"]
    length = int(p.get("length_ms", 0))
    delay  = int(p.get("delay_ms", 0))
    direction_deg = int(p.get("direction_deg", 0))
    eff_id = int(payload.get("effect_id", -1))

    waveform = E.FF_SINE
    period_ms = int(p.get("period_ms", 10))   # 例: 100Hz = 10ms
    magnitude = int(max(-32768, min(32767, p.get("magnitude", 20000))))
    offset    = int(max(-32768, min(32767, p.get("offset", 0))))
    phase_deg = int(p.get("phase_deg", 0))

    buf = bytearray()
    buf += _hdr(E.FF_PERIODIC, eff_id, direction_deg, length, delay)
    buf += struct.pack(_PER_FMT, waveform, period_ms, magnitude, offset, phase_deg)
    buf += _ENV_ZERO
    buf += struct.pack("I", 0)  # custom_len = 0
    return buf

def build_effect_condition(payload, kind):
    """kind: E.FF_SPRING or E.FF_DAMPER"""
    p = payload["params"]
    length = int(p.get("length_ms", 0))
    delay  = int(p.get("delay_ms", 0))
    direction_deg = int(p.get("direction_deg", 0))
    eff_id = int(payload.get("effect_id", -1))

    # 値域はデバイスによって最適が違う。まずは安全な中程度で。
    right_sat = int(p.get("right_saturation", 0x7FFF))
    left_sat  = int(p.get("left_saturation",  0x7FFF))
    right_coef= int(p.get("right_coeff", -5000))
    left_coef = int(p.get("left_coeff",  -5000))
    deadband  = int(p.get("deadband", 0))
    center    = int(p.get("center", 0))

    # condition は左右2本分（index 0/1）を続けて入れる
    cond = struct.pack(_COND_FMT, right_sat, left_sat, right_coef, left_coef, deadband, center)
    cond += struct.pack(_COND_FMT, right_sat, left_sat, right_coef, left_coef, deadband, center)

    buf = bytearray()
    buf += _hdr(kind, eff_id, direction_deg, length, delay)
    buf += cond
    return buf

def build_effect_rumble(payload, strong=False):
    p = payload["params"]
    length = int(p.get("length_ms", 0))
    delay  = int(p.get("delay_ms", 0))
    eff_id = int(payload.get("effect_id", -1))

    strong_mag = int(p.get("strong_magnitude", 0 if not strong else 0x7FFF))
    weak_mag   = int(p.get("weak_magnitude",   0 if strong else 0x7FFF))

    buf = bytearray()
    buf += _hdr(E.FF_RUMBLE, eff_id, 0, length, delay)
    buf += struct.pack(_RUM_FMT, strong_mag, weak_mag)
    return buf

# =========================================================
# アップロード／再生／停止／削除
# =========================================================
def upload_effect(psy_fd, effect_buf):
    try:
        fcntl.ioctl(psy_fd, EVIOCSFF, effect_buf, False)
    except OSError as e:
        if e.errno == 28:  # ENOSPC
            # psy_fd から evdev.InputDevice を再解決できないので、
            # 「仮想デバイス側」を引数で貰う設計に変える or クリア自体をやめる。
            # 簡易対処：スロット不足時はそのまま例外にして上位で判断。
            raise
        else:
            raise
    # id 書き戻しの読み出しはそのまま
    hdr = struct.unpack(_FF_HDR_FMT, effect_buf[:struct.calcsize(_FF_HDR_FMT)])
    _, eff_id, *_ = hdr
    return eff_id


    # id はヘッダ内（type(2), id(2) の位置）に書き戻される
    # _FF_HDR_FMT="HhH H H H H" → 先頭から 2 バイト後の s16 が id
    # struct.unpack で取り出す:
    hdr = struct.unpack(_FF_HDR_FMT, effect_buf[:struct.calcsize(_FF_HDR_FMT)])
    _, eff_id, *_ = hdr
    return eff_id

def start_effect(psy_fd, effect_id, repeat=1):
    os.write(psy_fd, _ie(E.EV_FF, effect_id, repeat))
    os.write(psy_fd, _ie(E.EV_SYN, E.SYN_REPORT, 0))

def stop_effect(psy_fd, effect_id):
    os.write(psy_fd, _ie(E.EV_FF, effect_id, 0))
    os.write(psy_fd, _ie(E.EV_SYN, E.SYN_REPORT, 0))

def erase_effect(psy_fd, effect_id):
    fcntl.ioctl(psy_fd, EVIOCRMFF, effect_id, False)


"""
ここから
FFB実装
"""

"""
サイズ問題に対処
"""

from ctypes import *

u16 = c_uint16
s16 = c_int16
u32 = c_uint32
s32 = c_int32
ptr = c_void_p   # ← 64bit では 8B

class ff_envelope(Structure):
    _fields_ = [
        ("attack_length", u16),
        ("attack_level",  u16),
        ("fade_length",   u16),
        ("fade_level",    u16),
    ]

class ff_trigger(Structure):
    _fields_ = [("button", u16), ("interval", u16)]

class ff_replay(Structure):
    _fields_ = [("length", u16), ("delay",  u16)]

class ff_constant_effect(Structure):
    _fields_ = [("level", s16), ("envelope", ff_envelope)]

class ff_ramp_effect(Structure):
    _fields_ = [("start_level", s16), ("end_level", s16), ("envelope", ff_envelope)]

class ff_periodic_effect(Structure):
    _fields_ = [
        ("waveform", u16),
        ("period",   u16),
        ("magnitude", s16),
        ("offset",    s16),
        ("phase",     u16),
        ("envelope",  ff_envelope),
        ("custom_len", u32),
        ("custom_data", ptr),    # ← ここが“ポインタ”
    ]

class ff_condition_effect(Structure):
    _fields_ = [
        ("right_saturation", u16),
        ("left_saturation",  u16),
        ("right_coeff",      s16),
        ("left_coeff",       s16),
        ("deadband",         u16),
        ("center",           s16),
    ]

class ff_rumble_effect(Structure):
    _fields_ = [("strong_magnitude", u16), ("weak_magnitude", u16)]

class ff_effect_u(Union):
    _fields_ = [
        ("constant",  ff_constant_effect),
        ("ramp",      ff_ramp_effect),
        ("periodic",  ff_periodic_effect),
        ("condition", ff_condition_effect * 2),  # ← 2本
        ("rumble",    ff_rumble_effect),
    ]

class ff_effect(Structure):
    _fields_ = [
        ("type",      u16),
        ("id",        s16),       # ※符号付き
        ("direction", u16),
        ("trigger",   ff_trigger),
        ("replay",    ff_replay),
        ("u",         ff_effect_u),
    ]

class uinput_ff_upload(Structure):
    _fields_ = [
        ("request_id", u32),
        ("retval",     s32),
        ("effect",     ff_effect),
        ("old",        ff_effect),
    ]

class uinput_ff_erase(Structure):
    _fields_ = [
        ("request_id", u32),
        ("retval",     s32),
        ("effect_id",  u32),
    ]




#　FF 変換ヘルパ
def _clamp_u16(v, hi=0x7FFF):
    if v < 0: v = 0
    if v > hi: v = hi
    return int(v)

def _clamp_s16(v):
    if v < -0x8000: v = -0x8000
    if v >  0x7FFF: v =  0x7FFF
    return int(v)

def _build_condition_pair_from_generic(kind: int, eff_in) -> "ff_effect":
    """
    kind: ecodes.FF_SPRING or ecodes.FF_DAMPER
    eff_in: uinput_ff_upload().effect の union を想定（ctypes で来るやつ）

    返り値: 物理に渡す ff_effect（condition[2] 両方を確実に初期化）
    """
    # ---- デフォルトの安全値（実機で安定しやすい初期値） ----
    # SPRING: 中央に戻す → 係数は負方向で強さ、サチュレーションは上限、デッドバンド少なめ
    # DAMPER: 速度抵抗 → 係数は負（速度に比例）でやや小さめ
    sat        = 0x6000
    dead      = 0x0400
    coeff_spr = -0x5000
    coeff_dmp = -0x3000

    # eff_in から値を取得できるなら取り出して使う（ゲーム/上流の意図を尊重）
    # ただし型や範囲は必ずクリップする
    try:
        # もし eff_in.condition[0 or 1] が既に入っているならそれを参照
        # （uinput から来る値は union の condition に入るケースと spring/damper 個別表現のケースがあります）
        pass
    except Exception:
        pass

    # 係数は種別で変える（SPRING/DAMPER は同じ struct を共有）
    if kind == ecodes.FF_SPRING:
        rc = lc = _clamp_s16(coeff_spr)
    else:
        rc = lc = _clamp_s16(coeff_dmp)

    rs = ls = _clamp_u16(sat)
    db = _clamp_u16(dead)
    ct = _clamp_s16(0)

    # ----- ff_effect を 2 軸ぶん埋める -----
    eff = ff_effect()
    eff.id        = eff_in.id              # 新規なら -1（0xFFFF）にしてもOK。あなたの実装の割当方針に合わせて。
    eff.type      = kind
    eff.direction = 0                      # 1 軸なら 0 固定で十分
    eff.replay.length = getattr(eff_in.replay, "length", 20000)
    eff.replay.delay  = getattr(eff_in.replay, "delay", 0)

    # X 軸（0）
    eff.u.condition[0].right_saturation = rs
    eff.u.condition[0].left_saturation  = ls
    eff.u.condition[0].right_coeff      = rc
    eff.u.condition[0].left_coeff       = lc
    eff.u.condition[0].deadband         = db
    eff.u.condition[0].center           = ct

    # Y 軸（1）— 未使用でも必ず初期化！
    eff.u.condition[1].right_saturation = rs
    eff.u.condition[1].left_saturation  = ls
    eff.u.condition[1].right_coeff      = rc
    eff.u.condition[1].left_coeff       = lc
    eff.u.condition[1].deadband         = db
    eff.u.condition[1].center           = ct

    return eff



# --- ここで初めて sizeof を使う ---
UP_SZ = sizeof(uinput_ff_upload)

_UI_UP_SZ = sizeof(uinput_ff_upload)
_UI_ER_SZ = sizeof(uinput_ff_erase)

logging.info(f"DEBUG sizeof(uinput_ff_upload)   ={_UI_UP_SZ}")
logging.info(f"DEBUG sizeof(uinput_ff_erase)    ={_UI_ER_SZ}")
logging.info(f"DEBUG sizeof(ff_effect)          ={sizeof(ff_effect)}")

logging.debug(f"サイズのセルフチェック")
logging.debug(f"sizeof(uinput_ff_upload)={ctypes.sizeof(uinput_ff_upload)}")
logging.debug(f"sizeof(uinput_ff_erase)={ctypes.sizeof(uinput_ff_erase)}")
logging.debug(f"sizeof(ff_effect)={ctypes.sizeof(ff_effect)}")

UINPUT_IOCTL_BASE = ord('U')

# === uinput_ff_device.py ==========================================
import os, fcntl, ctypes, ctypes.util, struct
from evdev import ecodes as E

# ==== uinput FF callback structs & ioctls (順序厳守) ====

import fcntl, ctypes, errno, time, os

# ---- ioctl マクロ（asm-generic/ioctl.h 相当）----
_IOC_NRBITS=8; _IOC_TYPEBITS=8; _IOC_SIZEBITS=14; _IOC_DIRBITS=2
_IOC_NRMASK=(1<<_IOC_NRBITS)-1
_IOC_TYPEMASK=(1<<_IOC_TYPEBITS)-1
_IOC_SIZEMASK=(1<<_IOC_SIZEBITS)-1
_IOC_DIRMASK=(1<<_IOC_DIRBITS)-1
_IOC_NRSHIFT=0
_IOC_TYPESHIFT=_IOC_NRSHIFT+_IOC_NRBITS
_IOC_SIZESHIFT=_IOC_TYPESHIFT+_IOC_TYPEBITS
_IOC_DIRSHIFT=_IOC_SIZESHIFT+_IOC_SIZEBITS
_IOC_NONE=0; _IOC_WRITE=1; _IOC_READ=2

def _IOC(dir_, type_, nr, size):
    return ((dir_<<_IOC_DIRSHIFT) |
            (type_<<_IOC_TYPESHIFT) |
            (nr<<_IOC_NRSHIFT) |
            (size<<_IOC_SIZESHIFT))

def _IOR(type_, nr, dtype_size):  return _IOC(_IOC_READ,              type_, nr, dtype_size)
def _IOW(type_, nr, dtype_size):  return _IOC(_IOC_WRITE,             type_, nr, dtype_size)
def _IOWR(type_, nr, dtype_size): return _IOC(_IOC_READ | _IOC_WRITE, type_, nr, dtype_size)



UINPUT_IOCTL_BASE = ord('U')  # 'U'

# --- uinput internal event codes (for FF upload/erase mirroring)
UI_FF_UPLOAD = 0x01
UI_FF_ERASE  = 0x02

# --- set-bit ioctl (_IOW('U', N, int)) ---
_UI_INT_SZ = ctypes.sizeof(ctypes.c_int)
UI_SET_EVBIT  = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 100, _UI_INT_SZ)
UI_SET_KEYBIT = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 101, _UI_INT_SZ)
UI_SET_RELBIT = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 102, _UI_INT_SZ)
UI_SET_ABSBIT = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 103, _UI_INT_SZ)
UI_SET_MSCBIT = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 104, _UI_INT_SZ)
UI_SET_LEDBIT = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 105, _UI_INT_SZ)
UI_SET_SNDBIT = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 106, _UI_INT_SZ)
UI_SET_FFBIT  = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 107, _UI_INT_SZ)
UI_SET_PHYS   = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 108, _UI_INT_SZ)  # 未使用なら無視
UI_SET_SWBIT  = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 109, _UI_INT_SZ)

UI_BEGIN_FF_UPLOAD = _IOWR(UINPUT_IOCTL_BASE, 200, _UI_UP_SZ)
UI_END_FF_UPLOAD   = _IOW (UINPUT_IOCTL_BASE, 201, _UI_UP_SZ)
UI_BEGIN_FF_ERASE  = _IOWR(UINPUT_IOCTL_BASE, 202, _UI_ER_SZ)
UI_END_FF_ERASE    = _IOW (UINPUT_IOCTL_BASE, 203, _UI_ER_SZ)

logging.info(f"Debug UI_BEGIN_FF_UPLOAD ={UI_BEGIN_FF_UPLOAD}")
logging.info(f"Debug UI_END_FF_UPLOAD   ={UI_END_FF_UPLOAD}")
logging.info(f"Debug UI_BEGIN_FF_ERASE  ={UI_BEGIN_FF_ERASE}")
logging.info(f"Debug UI_END_FF_ERASE    ={UI_END_FF_ERASE}")


class input_absinfo(ctypes.Structure):
    _fields_ = [
        ('value',      ctypes.c_int),
        ('minimum',    ctypes.c_int),
        ('maximum',    ctypes.c_int),
        ('fuzz',       ctypes.c_int),
        ('flat',       ctypes.c_int),
        ('resolution', ctypes.c_int),
    ]

class uinput_abs_setup(ctypes.Structure):
    _fields_ = [
        ('code',    ctypes.c_uint),
        ('absinfo', input_absinfo),
    ]

UI_ABS_SETUP = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 4, ctypes.sizeof(uinput_abs_setup))

# デバイスセットアップ
class input_id(ctypes.Structure):
    _fields_ = [
        ('bustype', ctypes.c_uint16),
        ('vendor',  ctypes.c_uint16),
        ('product', ctypes.c_uint16),
        ('version', ctypes.c_uint16),
    ]

class uinput_setup(ctypes.Structure):
    _fields_ = [
        ('id', input_id),
        ('name', ctypes.c_char * 80),
        ('ff_effects_max', ctypes.c_uint32),
    ]

UI_DEV_CREATE  = _IOC(_IOC_NONE,  UINPUT_IOCTL_BASE, 1, 0)
UI_DEV_DESTROY = _IOC(_IOC_NONE,  UINPUT_IOCTL_BASE, 2, 0)
UI_DEV_SETUP   = _IOC(_IOC_WRITE, UINPUT_IOCTL_BASE, 3, ctypes.sizeof(uinput_setup))


# ---------- input_event ----------
INPUT_EVENT_FMT = "llHHi"   # (tv_sec, tv_usec, type, code, value)

def _s16(x):  return int((int(x) + (1<<16)) % (1<<16) - (1<<15))  # 使わなければ削除可
def _s32(x):
    x = int(x)
    if x >  0x7fffffff: x =  0x7fffffff
    if x < -0x80000000: x = -0x80000000
    return x

def pack_ie(t, c, v):
    return struct.pack(INPUT_EVENT_FMT, 0, 0, int(t) & 0xFFFF, int(c) & 0xFFFF, _s32(v))

# 互換API（どこからでもこれを呼ぶ）
def write_input_event(fd, t, c, v):
    os.write(fd, pack_ie(t, c, v))


import os, fcntl, errno, struct, threading, time, logging
from evdev import ecodes

# --- 低レベル: ioctl を別スレで実行し、timeoutで見切るユーティリティ -------------------

def _ioctl_erase_worker(fd: int, eviocrmff: int, effect_id: int):
    """
    別スレッドで EVIOCRMFF を実行するワーカー。
    ※ EVIOCRMFF は int 引数（4バイト）なので struct.pack('i', ...) を使う。
    """
    para = struct.pack('i', int(effect_id))
    fcntl.ioctl(fd, eviocrmff, para, False)

def _erase_with_timeout(fd: int, eviocrmff: int, effect_id: int, timeout_sec: float) -> str:
    """
    EVIOCRMFF を timeout 付きで実行。
    - 成功: "ok"
    - EINVAL など: "skip"
    - タイムアウト: "timeout"
    """
    res_holder = {"res": None, "err": None}
    t = threading.Thread(
        target=lambda: _thread_entry(_ioctl_erase_worker, res_holder, fd, eviocrmff, effect_id),
        daemon=True
    )
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        # 注意: ioctl はカーネル内でブロックするので、スレッドは中断できない
        # ここでは「見切って先へ進む」設計
        logging.warning(f"Pys / EVIOCRMFF timeout id={effect_id} after {timeout_sec:.2f}s")
        return "timeout"

    err = res_holder["err"]
    if err is None:
        return "ok"

    # 代表的な errno を仕分け
    if isinstance(err, OSError):
        if err.errno in (errno.EINVAL,):
            #logging.debug(f"Pys / EVIOCRMFF skip id={effect_id}: 解放済み")
            return "skip"
        if err.errno in (errno.ETIMEDOUT,):
            logging.warning(f"Pys / EVIOCRMFF timed out id={effect_id}: {err}")
            return "timeout"
    logging.error(f"Pys / EVIOCRMFF failed id={effect_id}: {err}")
    return "fail"

def _thread_entry(fn, res_holder, *args):
    try:
        fn(*args)
        res_holder["res"] = True
    except Exception as e:
        res_holder["err"] = e


# --- 高レベル: 一括削除（前処理で GAIN=0 してから、0..max_id を削除） -------------------

def erase_all_ff_effects_async(
    dev, *,        # evdev.InputDevice を想定（物理ハンドル）
    max_id: int = 64,
    timeout_per_id: float = 0.50,
    sleep_between: float = 0.00,
    set_gain_zero_first: bool = True,
) -> dict:
    """
    物理デバイス上の FF エフェクトを「非同期＋タイムアウト付き」で順次削除。
    ここでは「非同期 = 削除ごとに別スレッドを立てて timeout 監視」。
    - dev.fd に対して ioctl(EVIOCRMFF, int) を実行
    - 先に GAIN=0 -> syn() で静音化（ハング回避に有効なことが多い）
    - 0..max_id を総当たり（EINVAL は無視）

    Returns:
      {"ok": n_ok, "skip": n_skip, "timeout": n_to, "fail": n_fail, "elapsed": sec}
    """
    stats = {"ok": 0, "skip": 0, "timeout": 0, "fail": 0}
    t0 = time.time()

    # 1) 先にゲインを 0（安全・静音措置）
    if set_gain_zero_first:
        try:
            dev.write(ecodes.EV_FF, ecodes.FF_GAIN, 0)
            dev.syn()
            logging.debug("Pys / gain=0 applied before erase-all")
            # 一部デバイスは直後のeraseで詰まりやすいので、ほんの少し待つ
            time.sleep(0.01)
        except Exception as e:
            logging.warning(f"Pys / gain=0 failed before erase-all: {e}")

    path = getattr(dev, "path", "?") or getattr(getattr(dev, "name", None), "__str__", lambda: None)() 
    logging.debug(f"Pys / erase_all_ff_effects_async: target={path}, max_id={max_id}")

    # 2) ID を順に削除（各IDにつき timeout 監視）
    for eff_id in range(int(max_id)):
        try:
            rc = _erase_with_timeout(dev.fd, EVIOCRMFF, eff_id, timeout_per_id)
            stats[rc] = stats.get(rc, 0) + 1
        except Exception as e:
            logging.error(f"Pys / EVIOCRMFF crash id={eff_id}: {e}")
            stats["fail"] += 1
        if sleep_between > 0:
            time.sleep(sleep_between)

    stats["elapsed"] = time.time() - t0
    logging.info(
        "Pys / Cleared effects: ok=%d, skip=%d, timeout=%d, fail=%d (%.2fs)",
        stats["ok"], stats["skip"], stats["timeout"], stats["fail"], stats["elapsed"]
    )
    return stats


def _u32_le(x: int) -> bytes:
    return (x & 0xffffffff).to_bytes(4, "little")


import threading, time, errno

class UInputFFDevice:
    def __init__(self, ui_caps, name: str, vid: int=None, pid: int=None, version: int=0x0100, ff_effects_max=64, enqueue_cb=None, ui_base_fd=None, ui_base_path=None, loop=None, phys_dev=None, phys_event_path=None,ff_mapper=None,us=None, **kwargs):
        logging.debug("[FFB] UInputFFDevice : __init__")
        """
         ui_base_fd:       /dev/uinput の fd
         ui_base_path:     表示用
         loop:             asyncio loop
         phys_dev:         evdev.InputDevice（物理ホイール）。あればこれを優先
         phys_event_path:  物理ホイールの /dev/input/eventX（phys_dev が無い時に使う）
        """
        self._last_const_key = None
        self._last_const_ts = None
        self.ui_caps = ui_caps
        
        self.ui_base_fd = ui_base_fd
        self.ui_base_path = ui_base_path
        self.loop = loop
        setup_signal_handlers(self.loop, us)
        
        self.ff_mapper: Optional[FfEvioMapper] = ff_mapper if ff_mapper is not None else FfEvioMapper()
        self.us = us
        
        self._phys_meta = {}
        
        self._last_up_cache = {}   # key: virt_id -> (bytes_signature, last_ts)
        self._dedup_window_ms = 8  # 短い同一更新はスキップ（必要に応じて 3〜15ms で調整）
        
        self._last_ff_end_ts = 0.0
        self._min_ff_gap_sec = 0.002  # 2ms 程度の最小間隔（必要なら 0.0 に）
        self._last_seen_req = (-1, -1)  # (request_id, effect.type)
        
        # --- 物理FDの確保 ---
        if phys_dev is not None and hasattr(phys_dev, "fd"):
             self.phys_fd = int(phys_dev.fd)
             self.phys_event_path = getattr(phys_dev, "path", "(unknown)")
        elif phys_event_path:
             # 読み書き可能で開く（FFB は write 必須。POLL 用に NONBLOCK）
             self.phys_fd = os.open(phys_event_path, os.O_RDWR | os.O_NONBLOCK)
             self.phys_event_path = phys_event_path
        else:
             raise RuntimeError("UInputFFDevice: no physical device given (phys_dev or phys_event_path required)")
        self.ff_mapper.phys_fd = self.phys_fd
        
        self._effect_types = {}       # effect_id -> ecodes.FF_*
        self._effects      = {}       # effect_id -> 最新の ff_effect（必要なら）
        self._ff_lock      = threading.Lock()
        self.ff_worker_stop = threading.Event()
        
        # 互換処理
        if vid is None and "vendor" in kwargs:  vid = kwargs["vendor"]
        if pid is None and "product" in kwargs: pid = kwargs["product"]

        # W) UnderSteer 機器への書き込み用
        self.ui_base_fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)
        #self.ui_base_fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)
        #self.ui_base_fd = os.open("/dev/uinput", os.O_RDWR)
        """
        # 既に open 済みでも後付けで nonblock にできる（保険）
        flags = fcntl.fcntl(self.ui_base_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.ui_base_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        """
        
        # R) UnderSteer ゲームから読み込み用
        # R) FF コールバックは “poll” の専用スレッドで待機
        logging.info(f"</dev/uinput>: FF request server start")
        self._ff_srv_stop = threading.Event()
        self._ff_srv_thr = threading.Thread(
            target=self._ff_request_server_loop, name="uinput-ff-server", daemon=True
        )
        # これを、後に回したい ui._ff_srv_thr.start()
        self._ff_srv_thr.start()
        
        # ---- 互換用の公開属性 ----
        self.name = name
        self.vid = vid
        self.pid = pid
        self.version = version
        self.device = None       # 後で eventX を解決して入れる（互換）
        self.event_path = None
        self.ui_event_fd = None

        # 2)
        # 基本EVの宣言（必要に応じて追加）
        self._setbit(UI_SET_EVBIT,  ecodes.EV_KEY)
        self._setbit(UI_SET_EVBIT,  ecodes.EV_ABS)
        # EV_FF は ui_caps が要求したときのみ expose（--ff-pass-through のみ）
        if ecodes.EV_FF in ui_caps:
            self._setbit(UI_SET_EVBIT, ecodes.EV_FF)
            #logging.debug("UI_SET_EVBIT/EV_FF Set OK")

        # --- KEY: そのまま expose ---
        for code in ui_caps.get(E.EV_KEY, []):
            #time.sleep(0.002)  # ★空振り時のCPU張り付き防止（2ms）
            self._setbit(UI_SET_KEYBIT, int(code))
        #logging.debug("UI_SET_KEYBIT/EV_KEY Set OK")

        # --- ABS: ビット + レンジ設定（AbsInfo を使う）---
        for code, ai in ui_caps.get(E.EV_ABS, []):
            self._setbit(UI_SET_ABSBIT, int(code))
            # AbsInfo(value,min,max,fuzz,flat,resolution)
            self._abs_setup(
                int(code),
                int(ai.min), int(ai.max),
                int(ai.fuzz), int(ai.flat), int(ai.resolution),
                int(ai.value),
            )
        #logging.debug("UI_SET_ABSBIT (self._abs_setup) Set OK")
        
        self.ffbDic = []
        if us.ff_passthrough_easy:
            minimumFFB_caps = {
                            E.FF_GAIN,
                            E.FF_AUTOCENTER,
                        }
        else:
            minimumFFB_caps = {
                            E.FF_GAIN,
                            E.FF_AUTOCENTER,
                            E.FF_CONSTANT,
                            E.FF_SPRING,
                            E.FF_DAMPER,
                            E.FF_RUMBLE,
                        }
        # 3)
        # FFB 能力は ui_caps に指定されたもののみ expose
        if ecodes.EV_FF in ui_caps:
            # full 指定
            for ff in ui_caps.get(ecodes.EV_FF, []):
            # minimum 指定
            #for ff in minimumFFB_caps:
                try:
                    self.ffbDic.append(int(ff))
                    self._setbit(UI_SET_FFBIT, int(ff))
                except OSError:
                    pass
        #logging.debug("UI_SET_FFBIT Set OK")

        # 既存の /dev/input/event* をスナップショット
        before = set(list_devices())

        # 4)
        # デバイスセットアップ → 作成
        us = uinput_setup()
        us.id = input_id(bustype=0x03, vendor=vid, product=pid, version=version)
        us.name = self.name.encode("utf-8")
        us.ff_effects_max = ff_effects_max
        
        raw = bytes(us)
        logging.debug("uinput_setup len={len(raw)}")
        logging.debug("hex:{raw.hex()}")

        # 5)
        fcntl.ioctl(self.ui_base_fd, UI_DEV_SETUP, us)
        logging.debug("UI_DEV_SETUP  OK")
        # 6)
        fcntl.ioctl(self.ui_base_fd, UI_DEV_CREATE)
        logging.debug("UI_DEV_CREATE  OK")
        CREATED_UI = True
        
        """
        作成後に能⼒を弄ると udev->state == UIST_CREATED で EINVAL になります。
        必ず CREATE 前に全部の能力宣言を終える。
        """
        
        # ★ 生成された eventX を特定して self.device に互換提供
        self.device = None  # evdev.UInput 互換プロパティ
        for _ in range(40):  # 最大 ~2秒待つ（50ms * 40）
            logging.debug(f"[uinput] waiting for {name}...")
            time.sleep(0.05)
            after = set(list_devices())
            new_nodes = sorted(after - before)
            for path in new_nodes:
                try:
                    idev = InputDevice(path)
                    if idev.name == name:
                        self.device = path           # 互換: evdev.UInput.device
                        self.event_path = path       # 明示名（お好みで）
                        self.ui_event_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)  # 使わなくてもOK
                        break
                except Exception:
                    continue
            if self.device:
                break

        if not self.device:
            # 見つからなくても致命ではないが、従来コードが path を期待しているなら警告しておく
            logging.error("[error] Could not resolve created event node for uinput device; self.device is None")
        logging.info(f"uinput device ... {self.device}")
        # ※ emit() は /dev/uinput の self.fd に write するので挙動はそのままです

    def _install_waker(self):
        self._w_r, self._w_w = os.pipe()
        for fd in (self._w_r, self._w_w):
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def _register_poll(self, p):
        p.register(self.ui_base_fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        p.register(self._w_r,  select.POLLIN)

    def _wake(self):
        try: os.write(self._w_w, b'X')
        except OSError: pass

    def stop(self):
        self._ff_srv_stop.set()

    def shutdown(self, timeout=2.0):
        self.stop()
        try: os.close(self.ui_base_fd)
        except OSError: pass
        # スレッド/Taskを待つ（一定時間で諦めてハード中断へ）
        for th in list(self._threads):
            th.join(timeout=timeout)

    def _start_watchdog(self, stall_sec=5.0):
        return
        #止めて様子見。タイミングで不具合のため
        self._last_ff_io = time.monotonic()
        def wd():
            while not self._ff_srv_stop.is_set():
                time.sleep(wacthdog_sec)
                if time.monotonic() - self._last_ff_io > stall_sec:
                    logging.debug("[WD] FFB stall detected (>%.1fs). Panic FLUSH.", stall_sec)
                    self._panic_ff_flush()
                    self._last_ff_io = time.monotonic()
        t = threading.Thread(target=wd, name="ff-watchdog", daemon=True)
        t.start()
        self._threads.add(t)

    def _mark_ff_progress(self):
        self._last_ff_io = time.monotonic()

    def _panic_ff_flush(self):
        try:
            # 1) master gain=0 / auto-center off（対応してるなら）
            self._apply_gain(0)
            self._set_autocenter(0)
        except Exception: pass
        try:
            # 2) EVIOCRMFFで known phys_id を片っ端から削除（タイムアウト付き）
            self.ff_mapper.erase_all_phys_slots(self.phys_fd, timeout_sec=0.5)
        except Exception: pass

    def _clear_physical_slots(self, max_id=64, timeout_per_id=0.25):
        """
        物理デバイス側(phys_fd)の FF スロットを可能な限り掃除。
        1) まず仮想→物理マップに残っている phys_id を優先的に削除
        2) 念のため 0..max_id-1 を総当り（EINVAL は無視）
        """
        logging.debug("_clear_physical_slots Start")
        t0 = time.monotonic()
        if getattr(self, "phys_fd", None) is None:
            logging.error("Pys / _clear_physical_slots: phys_fd is None")
            return

        ok = skip = fail = 0
        # 1) マッピングされたIDを優先して潰す
        for virt_id, phys_id in list(self.ff_mapper._virt2phys.items()):
            #logging.debug("Try now..")
            try:
                _ioctl_with_timeout(self.phys_fd, EVIOCRMFF, struct.pack('i', int(phys_id)), timeout_sec=timeout_per_id)
                ok += 1
                #logging.debug("[ff物理] ERASE map virt=%d phys=%d OK (%d Try)", virt_id, phys_id, cnt)
                # マップ掃除（同じ仮想IDが再利用される可能性もあるため、一応消す）
                #try:
                #    self.ff_mapper._virt2phys.pop(int(virt_id), None)  #eff_id 
                #except Exception:
                #    pass
                #return
            except OSError as e:
                if e.errno != errno.EINVAL:
                    #logging.debug("[ff物理] ERASE map virt=%d phys=%d skip: %s", virt_id, phys_id, e)
                    skip += 1
            except Exception as e:
                #logging.debug("[ff物理] ERASE map virt=%d phys=%d fail: %s", virt_id, phys_id, e)
                fail += 1
        logging.debug(f"[ff物理] ERASE map: (ok {ok} / skip {skip} / fail {fail})")
        self.ff_mapper._virt2phys.clear()
        logging.debug(f"_virt2phys.clear")


    def _ff_request_server_loop(self):
        import select
        self._make_uinput_nonblock()   # 既に open 済みでも後付けで nonblock にできる

        p = select.poll()
        p.register(self.ui_base_fd, select.POLLIN | select.POLLERR | select.POLLHUP)

        # --- 初回だけ初期化（ミューテックス化 & デバウンス） ---
        import threading, errno
        if not hasattr(self, "_ff_lock"):
            self._ff_lock = threading.Lock()
            self._last_ff_end_ts = 0.0
            self._min_ff_gap_sec = 0.002   # 2ms（0.0〜0.005で調整）
            self._last_seen_req = (-1, -1) # (request_id, effect.type)

        print(f"[LoopStart(U/FFB-Pys] <poll wait> {get_path_from_fd(self.ui_base_fd)} >>> Pys-Wheel")
        #logging.debug(f"LoopWait_ms: {LoopWait_ms}")
        while not self._ff_srv_stop.is_set():
            t0 = time.perf_counter_ns()
            #evs = p.poll(LoopWait_ms)
            dt_ms = (time.perf_counter_ns() - t0) / 1e6

            if self.us.DEBUG_TELEMETORY:
                logging.warning(f"[POLL] timeout ~{dt_ms:.3f} ms (no events)")
            # ここで “完全に” ドレインする（未処理を残さない）
            drained = 0
            while True:
                # Read Next
                kind, obj = self._try_begin_ff() # ここで読んでる！
                #time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
                if kind is None:
                    break

                flgSkip = False
                if kind == "UPLOAD":
                    up = obj    # UP オブジェクト
                    eff_t  = int(up.effect.type)
                    req_id = int(up.request_id)
                    logging.debug(f"path[ui_base_fd]={fd_path(self.ui_base_fd)}")
                    logging.debug(f"Pys / UI_BEGIN_FF_UPLOAD: type={FfEvioMapper._ff_type_name(eff_t)} req_id={req_id}")

                    # === ミューテックスで BEGIN→処理→END を不可分化 ===
                    with self._ff_lock:
                        began = True  # try_begin で既に BEGIN されている前提
                        try:
                            # --- 最小インターバルで過負荷を緩和（FH5対策） ---
                            now = time.monotonic()
                            dt  = now - self._last_ff_end_ts
                            if dt < self._min_ff_gap_sec:
                                time.sleep(self._min_ff_gap_sec - dt)

                            # --- (req_id,type) が直前と同一なら coalesce（成功扱いで返す） ---
                            #if (req_id, eff_t) == self._last_seen_req or (drained > 0 and req_id == 0):
                            #    up.retval = 0
                            #    logging.debug("COALESCE UPLOAD: same (req_id,type) or req_id==0 after first -> skip heavy work")
                            #else:
                            if  True:
                                try:
                                    # 実処理（物理側へ EVIOCSFF 等）
                                    self._handle_ff_upload(up)  # up.retval は内部で設定
                                except Exception as e:
                                    up.retval = -getattr(e, "errno", errno.EIO)
                                    logging.error("UPLOAD handling error errno=%s", getattr(e, "errno", "??"))
                                    os.exit(1)
                                #finally:
                                #    self._last_seen_req = (req_id, eff_t)

                            # --- END は必ず対で呼ぶ ---
                            try:
                                time.sleep(LoopWait_sec / 10) #fcntl.ioctl の前にも必要っぽい気がする
                                fcntl.ioctl(self.ui_base_fd, UI_END_FF_UPLOAD, up, True)
                                time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
                                logging.debug(f"Pys / UI_END_FF_UPLOAD: type={FfEvioMapper._ff_type_name(eff_t)} req_id={req_id}")
                            except OSError as e:
                                # EINVAL(22) 等は握り潰して継続（レース/二重END許容）
                                logging.warning("UI_END_FF_UPLOAD failed: %r ; continue", e)
                                time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
                            self._last_ff_end_ts = time.monotonic()
                        finally:
                            # 通常パスで END 済みならフォールバック不要
                            pass
                    drained += 1

                elif kind == "ERASE": # ERASE
                    er = obj    # ER オブジェクト
                    logging.debug(f"path[ui_base_fd]={fd_path(self.ui_base_fd)}")
                    logging.debug("Pys / UI_BEGIN_FF_ERASE (virt_id=%d)", int(er.effect_id))
                    # ERASE も同じロックで直列化（BEGIN→処理→END）
                    with self._ff_lock:
                        began = True
                        try:
                            try:
                                self._handle_ff_erase(er)  # 中で物理 id 解放など
                                #time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
                                er.retval = 0
                            except Exception as e:
                                er.retval = -getattr(e, "errno", errno.EIO)
                            try:
                                time.sleep(LoopWait_sec / 10) #fcntl.ioctl の前にも必要っぽい気がする
                                fcntl.ioctl(self.ui_base_fd, UI_END_FF_ERASE, er, True)
                                time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
                                logging.debug(f"Pys / UI_END_FF_ERASE: type={FfEvioMapper._ff_type_name(er.effect_id)} req_id={er.request_id}")
                            except OSError as e:
                                logging.error("Can not UI_END_FF_ERASE: %r (continue)", e)
                                os.exit(1)
                        finally:
                            # 通常パスで END 済みならフォールバック不要
                            pass
                    drained += 1

            # LoopEnd: UP,ER どっちも終わったらここに来る。
            # ドレイン有無に関わらず最後に 1 回だけ SYN
            if drained:
                logging.warning("[Psy Poll] drained %d FF requests", drained)
            # 初期化直後は無いのでIF文
            if CREATED_UI:
                #print(get_path_from_fd(self.ui_base_fd))
                write_input_event(self.ui_base_fd, E.EV_SYN, E.SYN_REPORT, 0)
            time.sleep(LoopWait_sec) # Loop Wait 4ms
        # LoopEnd: ドライバ終了のタイミングでここ

    import os, fcntl, select, errno, logging

    def _make_uinput_nonblock(self):
        fl = fcntl.fcntl(self.ui_base_fd, fcntl.F_GETFL)
        if not (fl & os.O_NONBLOCK):
            fcntl.fcntl(self.ui_base_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            logging.debug("Pys / uinput fd set O_NONBLOCK")

    def _try_begin_ff(self):
        """
        どちらが来ているかをノンブロッキングで判定。
        返り値: ("UPLOAD", up) / ("ERASE", er) / (None, None)
        """

        # 1) UPLOAD を先に試す（多い方を先に）
        up = uinput_ff_upload()
        try:
            fcntl.ioctl(self.ui_base_fd, UI_BEGIN_FF_UPLOAD, up, True)  # O_NONBLOCK なので無ければ EAGAIN
            return "UPLOAD", up
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINVAL):
                traceback.print_exc()
                #pass
                raise

        # 2) ERASE を試す
        er = uinput_ff_erase()
        try:
            fcntl.ioctl(self.ui_base_fd, UI_BEGIN_FF_ERASE, er, True)
            return "ERASE", er
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINVAL):
                traceback.print_exc()
                #pass
                raise

        began = True
        
        return None, None

    def _handle_ff_erase(self, er: "uinput_ff_erase"):
        virt_id = int(er.effect_id)
        logging.debug("Pys / BEGIN_ERASE req=%d virt_id=%d", int(er.request_id), virt_id)
        
        if True:
            phys_id = self.ff_mapper._virt2phys.pop(virt_id, -1)
            if phys_id >= 0:
                try:
                    fcntl.ioctl(self.phys_fd, EVIOCRMFF, phys_id, False)
                    #time.sleep(LoopWait_sec)
                    logging.warning(f"[FFB-Pys(Hdl)] EVIOCRMFF to physical: id={phys_id}")
                except OSError as e:
                    logging.error(f"[FFB-Pys(Hdl)] EVIOCRMFF failed id={phys_id}: {e}")
                self._phys_meta.pop(phys_id, None)
        er.retval = 0


    def _effect_key_for_compare(self, eff: ff_effect) -> bytes:
        
        ret = [
            eff.type,
            eff.direction,
            eff.trigger.button,
            eff.trigger.interval,
            eff.replay.length,
            eff.replay.delay,
        ]
        
        return ret

    # 直近キーと時刻で coalesce
    def _coalesce_and_maybe_skip_constant(self, upEff):
        # test ...
        #return False  # スキップ不可（処理する）
        
        if upEff.type != ecodes.FF_CONSTANT:
            return False  # スキップ不可（処理する）

        key = self._effect_key_for_compare(upEff)
        now = time.monotonic()

        # 変更なし
        if key == self._last_const_key:
            return True

        # 更新
        self._last_const_key = key
        self._last_const_ts = now
        return False

    def _handle_ff_upload(self, up: "uinput_ff_upload"):
        virt_id = int(up.effect.id)  # uinput から来た仮想ID（更新キー）
        eff = up.effect               # ctypes 構造体

        #if self._coalesce_and_maybe_skip_constant(up):
        #    return  # UI_END_FF_UPLOAD 側で up.retval を見て完了させる

        # --- 構造体全体をダンプ ---
        #detail = dump_ctypes_struct(up.effect)
        #logging.debug(f"[FFB-Pys(_handle_ff_upload)] effect DUMP:\n{detail}")

        if self.us.ff_passthrough_easy:
            if int(eff.type) == ecodes.FF_GAIN:
                logging.error("Gain xxxxxxxxxxxxxxxxxxxx")
            if int(eff.type) == ecodes.FF_AUTOCENTER:
                logging.error("AUTOCENTER xxxxxxxxxxxxxx")
            # 明確に不対応
            up.retval = -errno.EINVAL
            return
        
        t = int(eff.type)
        if t not in self.ffbDic:
            # 明確に不対応
            logging.error(f"明確に不対応 : type={t}")
            up.retval = -errno.EINVAL
            return
        
        # 1) 仮想→物理の既存割当を探す
        phys_id = self.ff_mapper._virt2phys.get(virt_id, None)
        if phys_id is not None:
            eff.id = phys_id
            is_update = True
        else:
            eff.id = -1
            is_update = False
        
        # 代わりに別名の作業変数を用意（ログ用）
        prev_phys_id = phys_id
        new_phys_id  = -1
        
        # BEGIN 直後
        logging.debug("Pys / BEGIN_UPLOAD req=%d type=%d id(virt?)=%d len=%d delay=%d",
                      up.request_id, int(up.effect.type), int(up.effect.id),
                      int(up.effect.replay.length), int(up.effect.replay.delay))
        logging.debug("Pys / BEGIN_UPLOAD req=%u type=%u vID=%d (ff.id before=%d)",
              int(up.request_id), int(eff.type), int(virt_id), int(eff.id))
        try:
            #logging.debug(f"[FFB-Pys(UP)] _handle_ff_upload: effect={FfEvioMapper._ff_type_name(eff.type)}")
            if int(eff.type) == ecodes.FF_CONSTANT:
                logging.debug(f"[FFB-Pys(UP)] effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = ecodes.FF_CONSTANT
                
                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで

            elif int(eff.type) == ecodes.FF_SPRING:
                logging.debug(f"[FFB-Pys(UP)] effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = ecodes.FF_SPRING
                
                # FF_SPRING , FF_DAMPER の場合 safe_eff 利用
                # ユニオンに入ってきたものを “安全に 2 軸初期化済み condition[2]” に組み直す
                safe_eff = _build_condition_pair_from_generic(t, eff)
                eff = safe_eff
                
                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで

            elif int(eff.type) == ecodes.FF_DAMPER:
                logging.debug(f"[FFB-Pys(UP)] effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = ecodes.FF_DAMPER
                
                # FF_SPRING , FF_DAMPER の場合 safe_eff 利用
                # ユニオンに入ってきたものを “安全に 2 軸初期化済み condition[2]” に組み直す
                safe_eff = _build_condition_pair_from_generic(t, eff)
                eff = safe_eff

                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで

            elif eff.type == ecodes.FF_FRICTION:
                logging.debug(f"[FFB-Pys(UP)] effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = ecodes.FF_FRICTION
                
                # FF_SPRING , FF_DAMPER の場合 safe_eff 利用
                # ユニオンに入ってきたものを “安全に 2 軸初期化済み condition[2]” に組み直す
                safe_eff = _build_condition_pair_from_generic(t, eff)
                eff = safe_eff

                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで

            elif int(eff.type) == ecodes.FF_RUMBLE:
                logging.debug(f"[FFB-Pys(UP)] effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = ecodes.FF_RUMBLE
                
                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで
                
            elif int(eff.type) == ecodes.UI_FF_UPLOAD:
                logging.debug(f"[FFB-Pys(UP)] effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = ecodes.UI_FF_UPLOAD
                
                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで

            elif int(eff.type) == ecodes.FF_PERIODIC:
                logging.debug(f"[FFB-Pys(UP)] effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = ecodes.FF_PERIODIC
                
                wave = eff.u.periodic.waveform
                if wave in (ecodes.FF_SINE, ecodes.FF_TRIANGLE, ecodes.FF_SQUARE):
                    logging.debug(f"[FFB-Pys(UP)] PERIODIC: waveform={wave} mag={eff.u.periodic.magnitude}")
                else:
                    logging.warning(f"[FFB-Pys(UP)] PERIODIC: Unknown waveform {wave}")

                # ★ custom未使用なら必ずゼロ化（機種/ドライバ依存のEINVAL回避）
                try:
                    eff.u.periodic.custom_len = 0
                    eff.u.periodic.custom_data = None
                except Exception:
                    pass

                status = _sanitize_periodic(eff)
                if status == "skip_zero":
                    # 「ゼロ強度の周期波」は実質無意味なので、物理送信せず成功扱いで返す
                    logging.debug("PERIODIC magnitude=0 → skip upload (pretend success)")
                    up.retval = 0
                    return

                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで

            else:
                # 未対応タイプ
                logging.error(f"[FFB-Pys(UP)] 未対応effect= {FfEvioMapper._ff_type_name(eff.type)} ")
                self._effect_types[int(eff.id)] = eff.type
                
                # 共通処理ここから
                new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                # 3) マップ更新（virt→phys, phys→virt）
                self.ff_mapper._virt2phys[virt_id] = new_phys_id
                self.ff_mapper._phys2virt[new_phys_id] = virt_id
                logging.debug(f"[FFB-Pys(UP)] {FfEvioMapper._ff_type_name(eff.type)} to physical: id={new_phys_id}")
                up.effect.id = int(virt_id)
                up.retval = 0
                # 共通処理ここまで

            # _handle_ff_upload の成功パスの最後（物理 id 割当後）
            #up.effect = eff
            logging.debug(f"Pys / UPLOAD mapped virt={virt_id} -> phys={phys_id} new_phys={new_phys_id} (type={eff.type})")
        except OSError as e:
            if e.errno == errno.ENOSPC:
                # まずLRUで整理
                freed = self.ff_mapper._evict_some_phys_slots(limit=4)
                if freed == 0:
                    # まだダメなら総当りでさらに整理（上の実装が既に総当りを含むので不要なら省略可）
                    pass
                # リトライ（1回だけ）
                phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, up.effect)
                logging.warning("ENOSPC: freed <%d> slots, retrying alloc", freed)
                try:
                    eff.id = -1
                    new_phys_id = self.ff_mapper.upload_ff_effect_via_eviocsff(self.phys_fd, eff)
                    self.ff_mapper._virt2phys[virt_id] = new_phys_id
                    self.ff_mapper._phys2virt[new_phys_id] = virt_id
                    up.retval = 0
                except OSError as e2:
                    up.retval = -getattr(e2, "errno", errno.EIO)
                    logging.error("ERROR:_handle_ff_upload / 003")
                    traceback.print_exc()
                    raise
            else:
                up.retval = -getattr(e, "errno", errno.EIO)
                logging.error("ERROR:_handle_ff_upload / 002")
                traceback.print_exc()
                raise
        except Exception as e:
            logging.error("ERROR:_handle_ff_upload / 001")
            traceback.print_exc()
            raise

    def bind_ff_enqueue(self, cb):
        """後からバインドする用（UnderSteer が ff_worker を作った後に呼べる）"""
        self._enqueue_cb = cb
        # 溜めていたものを吐き出す
        if self._pending:
            for kind, payload in self._pending:
                try:
                    cb(kind, payload)
                except Exception as e:
                    logging.error("enqueue flush failed: %s", e)
            self._pending.clear()

    def _enqueue_from_thread(self, kind: str, payload: dict):
        cb = self._enqueue_cb
        if cb is None:
            # まだ ff_worker が準備できてない間は溜める（落とさない）
            self._pending.append((kind, payload))
            return
        try:
            cb(kind, payload)     # ※ UnderSteer 側で thread-safe にしておく
        except Exception as e:
            logging.error("enqueue failed: %s", e)

    def _setbit(self, which, code):
        # 第3引数は “int 値” でOK（_IOW の「copy_from_user(int)」に一致）
        #logging.debug(f"_setbit %d %d", which, code)
        fcntl.ioctl(self.ui_base_fd, which, int(code))

    # --- evdev.UInput 互換APIを追加 ---
    def write(self, type_, code, value):
        write_input_event(self.ui_base_fd, type_, code, value)

    def syn(self):
        write_input_event(self.ui_base_fd, E.EV_SYN, E.SYN_REPORT, 0)

    def emit(self, type_, code, value):
        self.write(type_, code, value)
        #time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
        if type_ != E.EV_SYN:
            self.syn()
            # ここ入れて様子見 key emit...
            time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要

    def close(self):
        """
        仮想デバイスの後始末：
          1) FFサーバ停止
          2) UI_DEV_DESTROY を発行
          3) event_fd / uinput fd をクローズ
        何度呼んでも安全（idempotent）。
        """
        # 1) FFコールバック・スレッド停止
        try:
            self.stop_ff_server()
        except Exception:
            pass

        # 2) デバイス破棄（存在すれば）
        try:
            if hasattr(self, "fd") and self.fd is not None:
                try:
                    fcntl.ioctl(self.ui_base_fd, UI_DEV_DESTROY, 0, False)
                except OSError:
                    # 既に destroy 済み / 作成失敗時などは無視
                    pass
        finally:
            # 3) FD クローズ
            try:
                if getattr(self, "ui_event_fd", None):
                    os.close(self.ui_event_fd)
                    self.ui_event_fd = None
            except Exception:
                pass
            try:
                if getattr(self, "ui_base_fd", None):
                    os.close(self.ui_base_fd)
                    self.ui_base_fd = None
            except Exception:
                pass

    def _abs_setup(self, code, minimum, maximum, fuzz=0, flat=0, resolution=0, value=0):
        setup = uinput_abs_setup()
        setup.code = int(code)
        setup.absinfo = input_absinfo(
            value, int(minimum), int(maximum),
            int(fuzz), int(flat), int(resolution)
        )
        fcntl.ioctl(self.ui_base_fd, UI_ABS_SETUP, setup)



"""
FFB実装
ここまで
"""



def code_to_name(code: int) -> str:
    """
    EV_KEY の数値コードから、見やすい名前（BTN_* / KEY_*）を返す。
    - まず BTN 辞書（code→'BTN_*'）を優先
    - 次に KEY 辞書（code→'KEY_*'）
    - それでも無ければ ecodes.ecodes（name→code）を総当たりで逆引き
    - 最後の手段で 'KEY_<code>'
    """
    # 確実ルート: code→name の辞書を直接引く
    name = ecodes.BTN.get(code)
    if not name:
        name = ecodes.KEY.get(code)
    if not name:
        # まれに辞書に欠けがある環境向けの保険（低頻度なので線形探索でOK）
        for n, c in ecodes.ecodes.items():  # name -> code
            if c == code:
                name = n
                break
    return name or f"KEY_{code}"

def get_vid_pid(phys_or_uniq: str) -> Tuple[Optional[str], Optional[str]]:
    """
    'usb-0000:02:00.0-1.2.1/input0' や uniq='045e:028e' のような文字列から
    それっぽい 16進の 'vid:pid' を抜き出す（見つからなければ (None, None)）。
    """
    m = re.search(r'([0-9a-fA-F]{4}):([0-9a-fA-F]{4})', phys_or_uniq or "")
    if m:
        return m.group(1).lower(), m.group(2).lower()
    return (None, None)

@dataclass
class DevInfo:
    path: str
    name: str
    phys: str
    uniq: str
    hidraw_path: str
    vendor: Optional[str]
    product: Optional[str]
    dev: InputDevice

def enumerate_input() -> List[DevInfo]:
    logging.debug("enumerate_input")
    infos: List[DevInfo] = []
    for path in list_devices():
        dev = InputDevice(path)
        name = dev.name or ""
        phys = dev.phys or ""
        uniq = dev.uniq or ""
        hidraw_path = ""
        # uniq優先で vid:pid 抜く。なければ phys から推測
        vid, pid = get_vid_pid(uniq)
        if not vid or not pid:
            v2, p2 = get_vid_pid(phys)
            vid = vid or v2
            pid = pid or p2
        infos.append(DevInfo(path, name, phys, uniq, hidraw_path, vid, pid, dev))
    return infos

def fmt_info(i: DevInfo) -> str:
    vp = f"{i.vendor}:{i.product}" if (i.vendor and i.product) else "--:--"
    return f"{i.path:>15} | {vp} | name='{i.name}' phys='{i.phys}' uniq='{i.uniq}'"

def is_axis(code: int) -> bool:
    return ecodes.EV_ABS == ecodes.EV_ABS and code in ecodes.ABS.values()

def is_button(code: int) -> bool:
    # おおまかに BTN_* 系（キーボードキーは除外）
    return code in ecodes.BTN.values()

# ------------------------
# ギアマッピング
# ------------------------

def _name_to_code(self, name: str) -> int:
    n = name.strip()

    # まず ecodes.KEY / BTN の辞書を優先的に調べる
    for table in (ecodes.KEY, ecodes.BTN):
        if n in table:
            return table[n]

    # モジュール直下にも int 定数がある場合（古い evdev）
    val = getattr(ecodes, n, None)
    if isinstance(val, int):
        return val

    # 数値指定（10進 or 0x16進）にも対応
    try:
        return int(n, 0)
    except Exception:
        raise ValueError(f"Unknown button/key name: {name}")

class GearMapper:
    """
    テキスト定義からギア判定を行い、標準化した出力ボタン（BTN_0..BTN_7 / NEUTRAL=BTN_DEAD）を合成する。
    - 任意の物理ボタン名をギアに割当可能
    - 複数ボタン同時押しのギア定義も可（例: G1=BTN_0BTN_1）
    """
    STD_GEAR_CODES = [
        ecodes.BTN_0, ecodes.BTN_1, ecodes.BTN_2, ecodes.BTN_3,
        ecodes.BTN_4, ecodes.BTN_5, ecodes.BTN_6, ecodes.BTN_7,
    ]
    STD_NEUTRAL = ecodes.BTN_DEAD

    # ニュートラルフラグ（HAT-Keyboard連携用）
    neutralFlg = True

    def __init__(self, path: Path):
        self.path = path
        # gear_requirements[i] = set of input key codes for Gi (i:0..7)
        self.gear_requirements: List[Set[int]] = []
        self.neutral_button: Optional[int] = None  # 明示定義があれば使用
        # 入力側で監視すべきコード
        self.watch_codes: Set[int] = set()
        # 入力の現在押下状態（True/False）
        self.input_pressed: Dict[int, bool] = {}
        # 出力（標準化ボタン）の状態
        self.out_pressed: Dict[int, bool] = {c: False for c in (self.STD_GEAR_CODES + [self.STD_NEUTRAL])}
        self._load()

    # === 追加：名前→コード解決 ===
    def _name_to_code(self, token: str) -> int:
        """
        'BTN_2', 'KEY_1', 'B2', '258', '0x102' などを evdev のコード(int)へ変換。
        例外時は ValueError を投げる。
        """
        if token is None:
            raise ValueError("empty token")

        t = token.strip()
        if not t:
            raise ValueError("empty token")

        # 数値（10進/16進）
        if t.lower().startswith("0x"):
            return int(t, 16)
        if re.fullmatch(r"\d+", t):
            return int(t, 10)

        # 正規化
        t_norm = t.upper().replace("-", "_")

        # 'B<n>' を 'BTN_<n>' とみなす
        m = re.fullmatch(r"B(\d+)", t_norm)
        if m:
            t_norm = f"BTN_{m.group(1)}"

        # evdev 名で引く
        if t_norm in ecodes.ecodes:
            return ecodes.ecodes[t_norm]

        # KEY_<digit> の別名（念のため）
        m = re.fullmatch(r"KEY_(\d+)", t_norm)
        if m and t_norm in ecodes.ecodes:
            return ecodes.ecodes[t_norm]

        raise ValueError(f"unknown button/key name: {token}")

    def _add_gear_by_tokens(self, tokens: List[str]):
        # tokens は [ "BTN_X", "BTN_YBTN_Z", ... ] のような 1要素期待
        if not tokens:
            return
        spec = tokens[0]
        req = set(self._name_to_code(t) for t in spec.split("\t"))
        self.gear_requirements.append(req)
        self.watch_codes.update(req)

    def _load(self):
        txt = self.path.read_text(encoding="utf-8")
        tmp_requirements: List[Set[int]] = []
        for raw_line in txt.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # 形式いろいろ受け付ける
            if "=" in line or ":" in line:
                k, v = re.split(r"[:=]", line, maxsplit=1)
                key = k.strip().upper()
                val = v.strip()
                if key in ("G1","G2","G3","G4","G5","G6","G7","G8"):
                    req = set(self._name_to_code(t) for t in re.split(r"[\t\s]+", val) if t)
                    tmp_requirements.append(req)
                    self.watch_codes.update(req)
                elif key in ("NEUTRAL","N"):
                    self.neutral_button = self._name_to_code(val)
                    self.watch_codes.add(self.neutral_button)
                else:
                    # キー指定っぽいが無視
                    pass
            else:
                # プレーン列挙（順番がギア番号）
                self._add_gear_by_tokens([line])
        if tmp_requirements:
            self.gear_requirements = tmp_requirements
        # input_pressed 初期化
        for code in self.watch_codes:
            self.input_pressed[code] = False

    def feed_input_key(self, code: int, value: int) -> bool:
        """
        監視対象の入力キーが変化したら呼ぶ。
        戻り値: 出力の状態に変化があったかどうか。
        """
        if code not in self.watch_codes:
            return False
        self.input_pressed[code] = (value != 0)
        return self._recompute_output()

    def _recompute_output(self) -> bool:
        # 現在満たされているギア（先勝ち）
        active_gear_idx = None
        for i, req in enumerate(self.gear_requirements):
            if all(self.input_pressed.get(c, False) for c in req):
                active_gear_idx = i
                break
        # ニュートラルの判定
        neutral = False
        if active_gear_idx is None:
            if self.neutral_button is not None:
                neutral = self.input_pressed.get(self.neutral_button, False)
            else:
                neutral = True  # どのギア条件も満たさない＝ニュートラル
        GearMapper.neutralFlg = neutral

        # 目標出力状態を作る
        desired: Dict[int, bool] = {c: False for c in self.out_pressed.keys()}
        if active_gear_idx is not None and active_gear_idx < len(self.STD_GEAR_CODES):
            desired[self.STD_GEAR_CODES[active_gear_idx]] = True
        elif neutral:
            desired[self.STD_NEUTRAL] = True

        changed = (desired != self.out_pressed)
        self.out_pressed = desired
        return changed

    def emit_to(self, ui: UInput):
        """self.out_pressed の差分を実出力（押下/解放）として送る。"""
        for code, pressed in self.out_pressed.items():
            ui.write(ecodes.EV_KEY, code, 1 if pressed else 0)
            #time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
        ui.syn()
        # ここ入れて様子見 key emit...
        time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要

# ------------------------
# キーボードマッピング（TSV）
# ------------------------

class KeymapTSV:
    """
    TSV で定義した「入力ボタン → 出力キー（同時押し可）」のマッピングを、
    物理ボタンの押下/解放に同期して仮想キーボードへ送る。

    TSV 形式:
        <INPUT_BTN> \t <KEY_A(KEY_B...)>
    例:
        BTN_0\tKEY_SPACE
        BTN_4\tKEY_LEFTCTRLKEY_R

    - 押しっぱ対応: 入力が押下されている間は出力キーも押下状態を保持する
    - 解除順は逆順（修飾 → 本体の順で押し、本体 → 修飾の順で離すと安全）
    """
    def __init__(self, tsv_path: Path):
        self.tsv_path = tsv_path
        # 入力(数値コード: BTN/KEY) → [出力キーコード]
        self.map_codes: Dict[int, List[int]] = {}
        self.src_pressed_codes: Dict[int, bool] = {}
        # 入力(名前: HAT0_LEFT 等) → [出力キーコード]
        self.map_names: Dict[str, List[int]] = {}
        self.src_pressed_names: Dict[str, bool] = {}
        # 仮想キーボード UInput
        self.kb: Optional[UInput] = None

        self._load()
        self._open_uinput_keyboard()

    def _name_to_code(self, name: str) -> int:
        """'BTN_*' / 'KEY_*' / 数値 を evdev コード(int)へ"""
        from evdev import ecodes
        n = name.strip()
        # KEY/BTN 辞書を優先
        for tbl in (ecodes.KEY, ecodes.BTN):
            if n in getattr(tbl, "__members__", {}):
                return tbl.__members__[n]
            if n in tbl:
                return tbl[n]
        val = getattr(ecodes, n, None)
        if isinstance(val, int):
            return val
        try:
            return int(n, 0)
        except Exception:
            raise ValueError(f"Unknown button/key name: {name}")

    def _parse_keys(self, rhs: str) -> List[int]:
        keys = []
        for token in rhs.strip().split("\t"):
            token = token.strip()
            if not token:
                continue
            keys.append(self._name_to_code(token))
        return keys

    def _load(self):
        import re
        # 文字コード：UTF-8(BOM可) → CP932 の順で試す
        try:
            text = self.tsv_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = self.tsv_path.read_text(encoding="cp932")

        for idx, raw in enumerate(text.splitlines(), start=1):
            orig = raw
            line = raw.strip()
            if not line:
                continue
            # 行末コメントを除去（例: BTN_0 \t KEY_ENTER  # jump）
            if "#" in line:
                line = line.split("#", 1)[0].rstrip()
                if not line:
                    continue

            ## 実タブが無いが、可視「\t」がある場合は置換
            #if "\t" not in line and "\\t" in line:
            #    line = line.replace("\\t", "\t")

            # まずはタブで 2 分割（LHS / RHS）
            lhs = rhs = None
            if "\t" in line:
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    lhs, rhs = parts[0].strip(), parts[1].strip()

            #if not lhs or not rhs:
            #    logging.warning(f"[keymap] L{idx}: skip (no separator) : {orig}")
            #    continue
            print(lhs , " and " , rhs)
            try:
                dst_keys = self._parse_keys(rhs)
                if not dst_keys:
                    logging.warning(f"[keymap] L{idx}: no dst keys : {orig}")
                    continue
                # HAT 名はそのまま名前マップへ、他はコード化してコードマップへ
                if re.match(r"^HAT\d_(LEFT|RIGHT|UP|DOWN)$", lhs):
                    key = lhs
                    self.map_names[key] = dst_keys
                    self.src_pressed_names[key] = False
                else:
                    code = self._name_to_code(lhs)  # BTN_*/KEY_* or number
                    self.map_codes[code] = dst_keys
                    self.src_pressed_codes[code] = False
            except Exception as e:
                logging.warning(e)
                logging.warning(f"[keymap] L{idx}: skip : {orig}  (can not map)")
                continue

    def _open_uinput_keyboard(self):
         # 使うキーだけ expose（過不足があると send 時に失敗するため union を作る）
        all_keys: Set[int] = set()
        if self.map_codes:
            all_keys |= set(chain.from_iterable(self.map_codes.values()))
        if self.map_names:
            all_keys |= set(chain.from_iterable(self.map_names.values()))
        if not all_keys:
            return
        caps = {
             ecodes.EV_KEY: sorted(list(all_keys)),
             # EV_REP は不要（OS 側に任せる）
        }
        # Generic 仮想キーボード
        self.kb = UInput(caps, name="UnderSteer Virtual Keyboard",
                          vendor=0x16c0, product=0x27db, version=0x0100)
        logging.info(f"[uinput] created virtual keyboard: {self.kb.device}")

    @property
    def watch_codes(self) -> Set[int]:
        return set(self.map_codes.keys())

    @property
    def watch_names(self) -> Set[str]:
        return set(self.map_names.keys())

    def handle_src_event(self, code: int, value: int):
         """
         入力イベント（EV_KEY）を受け、マッピングされていれば kb に押下/解放を送る。
         value: 0=UP, 1=DOWN, 2=REPEAT（2は無視）
         """
         if self.kb is None:
             return
         if code not in self.map_codes:
             return
         if value == 2:
             # 自分でリピートを作らず、OS の auto-repeat に任せる
             return
 
         keys = self.map_codes[code]
         if value:
             # 押下：修飾から本体へ（記述順をそのまま採用）
             for k in keys:
                 self.kb.write(ecodes.EV_KEY, k, 1)
             self.kb.syn()
             self.src_pressed_codes[code] = True
         else:
             # 解放：逆順で解放（本体→修飾）
             for k in reversed(keys):
                 self.kb.write(ecodes.EV_KEY, k, 0)
             self.kb.syn()
             self.src_pressed_codes[code] = False

    def handle_named(self, name: str, pressed: bool):
        """
         名前入力（HAT0_LEFT 等）を受け、対応キーを押下/解放。
        """
        if self.kb is None:
            #logging.error("None...")
            return
        if name not in self.map_names:
            #logging.error("not in")
            return
        keys = self.map_names[name]
        if pressed:
            for k in keys:
                self.kb.write(ecodes.EV_KEY, k, 1)
            self.kb.syn()
            self.src_pressed_names[name] = True
        else:
            for k in reversed(keys):
                self.kb.write(ecodes.EV_KEY, k, 0)
            self.kb.syn()
            self.src_pressed_names[name] = False

    def close(self):
         try:
             if self.kb:
                 self.kb.close()
         except Exception:
             pass

# ------------------------
# デバイス選定
# ------------------------

def pick_device(infos: List[DevInfo], keyword: str) -> Optional[DevInfo]:
    kw = keyword.lower()
    for i in infos:
        if kw in (i.name or "").lower():
            return i
    return None

# ------------------------
# 仮想デバイス 定義生成
# ------------------------

def merge_capabilities(
    wheel: InputDevice, shifter: InputDevice,
    force_keys: Optional[List[int]] = None,
    expose_ff: bool = False,
    ignore_ffb: str = None,
    ff_off: bool = False,
    us = None,
) -> Tuple[Dict[int, List], Dict[int, AbsInfo]]:

    """
    2つの物理デバイスの capabilities() をマージ。
    - EV_ABS: それぞれの AbsInfo を union
    - EV_KEY: ボタン union
    - EV_FF : 仮想でも expose（最小限: FF_GAIN, FF_AUTOCENTER）※実験的
    """
    zero_fuzz   = True
    force_flat0 = True
    
    cap_w = wheel.capabilities(absinfo=True)
    cap_s = shifter.capabilities(absinfo=True)
    
    keys: Set[int] = set()
    abs_list: Dict[int, AbsInfo] = {}
    ignoreArr = ignore_ffb.strip().split(",")
    
    us.register_abs_mapping_first_win("wheel", cap_w)
    us.register_abs_mapping_first_win("shift", cap_s)
    
    #print("てすと")
    #print(ignoreArr)
    w_abs_list = []
    
    def take_abs(source):
        abs_caps = source.get(ecodes.EV_ABS, [])
        for code, absinfo in abs_caps:
            w_abs_list.append(ecodes.ABS[code])
            # すでにある場合は先勝ちで何もしない
            if code in abs_list:
                pass
            else:
                # 新規登録時も flat=0 / fuzz=0 に正規化
                fuzz = 0 if zero_fuzz else absinfo.fuzz
                flat = 0 if force_flat0 else absinfo.flat
                dst_min, dst_max = absinfo.min, absinfo.max
                mul = 1
                # 物理が -1..1（または 0..1）の「正規化済み」軸なら仮想側で 16bit レンジへ拡大
                if absinfo.min == 0 and absinfo.max == 1:
                    dst_min, dst_max, mul = 0, 32767, 32767
                abs_list[code] = AbsInfo(
                    value=0, min=dst_min, max=dst_max,
                    fuzz=fuzz, flat=flat, resolution=absinfo.resolution
                )
                # 後段のイベント拡大用メモ
                if us is not None and mul != 1:
                    us._axis_scale[code] = (absinfo.min, absinfo.max, mul)

    def take_keys(source):
        for code in source.get(ecodes.EV_KEY, []):
            if isinstance(code, (list, tuple)):
                keys.update(code)
            else:
                keys.add(code)

    # ホイール・シフター それぞれ取得
    take_abs(cap_w); take_abs(cap_s)
    take_keys(cap_w); take_keys(cap_s)

    uniq = sorted(set(w_abs_list))
    print(f"[i] uniq: axes={len(uniq)} → {', '.join(uniq)}")

    # FFB: --ff-pass-through のときだけ expose（それ以外は露出しない）
    ff_features: List[int] = []
    if expose_ff:
        cap_w_ff = cap_w.get(ecodes.EV_FF, [])
        # evdev の返し方が環境で異なるので flatten
        ff_features_set: Set[int] = set()
        for item in cap_w_ff:
            if str(item) in ignoreArr:
                pass
            else:
                if isinstance(item, (list, tuple)):
                    ff_features_set.update(item)
                else:
                    ff_features_set.add(item)
        ff_features = sorted(list(ff_features_set))
        print("")
        logging.debug("ff_features")
        logging.debug(ff_features)

    # 必要なら強制的に特定キーを expose（標準ギア出力など）
    if force_keys:
        keys.update(force_keys)

    ui_caps: Dict[int, List] = {
        ecodes.EV_KEY: sorted(list(keys)),
        ecodes.EV_ABS: [(code, absinfo) for code, absinfo in abs_list.items()],
    }
    
    if not ff_off:
        if ff_features:
            ui_caps[ecodes.EV_FF] = ff_features
    return ui_caps, abs_list


# --- ユーティリティ ---
def open_wheel_event_fd(wheel_info, *, require_ff=True):
    """
    wheel_info から物理ホイールの event ノードを見つけて O_RDWR で開く。
    - wheel_info.event_path があるならそれを使う
    - 無ければ wheel_info.dev (evdev.InputDevice) から path を取る
    - さらに無ければ /dev/input/by-id を走査して 'wheel' を含むものを探す
    return: (phys_fd:int, event_path:str)
    """
    # 1) 優先: 既知のパス
    cand = getattr(wheel_info, "event_path", None)
    if not cand and hasattr(wheel_info, "dev") and isinstance(wheel_info.dev, InputDevice):
        cand = wheel_info.dev.path  # 例: '/dev/input/event5'

    # 2) フォールバック: /dev/input/by-id を走査
    if not cand:
        byid = "/dev/input/by-id"
        if os.path.isdir(byid):
            for name in sorted(os.listdir(byid)):
                p = os.path.join(byid, name)
                try:
                    if "event" in os.readlink(p) and ("wheel" in name.lower() or "racing" in name.lower()):
                        cand = os.path.realpath(p)
                        break
                except Exception:
                    continue

    if not cand or not os.path.exists(cand):
        raise FileNotFoundError("物理ホイールの event ノードが見つかりません")

    # 3) 必要なら FFB サポートを確認
    if require_ff:
        try:
            dev = InputDevice(cand)
            caps = dev.capabilities(verbose=False)
            ff_caps = caps.get(ecodes.EV_FF, [])
            if not ff_caps:
                raise RuntimeError(f"{cand} が EV_FF をサポートしていません")
        except Exception as e:
            raise RuntimeError(f"EV_FF 確認に失敗: {e}")

    # 4) O_RDWR で開く（FFBの ioctl/write に必要）
    fd = os.open(cand, os.O_RDWR | os.O_NONBLOCK)
    return fd, cand


def find_hidraw_for_event(event_path: str, *, timeout_sec=0.03, max_up=6) -> str | None:
    """
    /dev/input/eventX -> /dev/hidrawY を最短経路で探索。
    - /sys/devices/virtual/... は仮想なので即 None を返す
    - 再帰glob禁止、上位 N 階層だけを確認
    - hidraw が見つかったら /dev/hidraw* を返す
    """
    ev = os.path.basename(event_path)
    base = os.path.realpath(f"/sys/class/input/{ev}")
    devdir = os.path.join(base, "device")

    # 1) 仮想デバイスなら即終了（hidrawは無い）
    if os.path.realpath(devdir).startswith("/sys/devices/virtual/"):
        return None

    # 2) まず最短の定位置: /sys/class/input/eventX/device/hidraw/hidraw*
    hid_base = os.path.join(devdir, "hidraw")
    if os.path.isdir(hid_base):
        try:
            for name in os.listdir(hid_base):
                dev = f"/dev/{name}"
                if os.path.exists(dev):
                    return dev
        except FileNotFoundError:
            pass

    # 3) 親を限定回数だけ辿って hidraw を探す（深追いしない）
    cur = devdir
    deadline = time.monotonic() + timeout_sec
    for _ in range(max_up):
        if time.monotonic() > deadline:
            break
        cur = os.path.dirname(cur)
        if not cur or cur == "/" or not os.path.isdir(cur):
            break
        hb = os.path.join(cur, "hidraw")
        if os.path.isdir(hb):
            try:
                for name in os.listdir(hb):
                    dev = f"/dev/{name}"
                    if os.path.exists(dev):
                        return dev
            except FileNotFoundError:
                pass

    # 見つからない＝hidraw非対応（たとえばBluetooth, 一部ドライバ, あるいは仮想）
    return None




# ------------------------
# パイプライン
# ------------------------

class UnderSteer:
    # 物理側: (role, src_abs) -> {min,max}
    _abs_src_meta: dict[tuple[str,int], dict]
    # 仮想側: vabs -> {min,max,center,dz_raw}（先勝ちを基準にする）
    # vABS -> {min, max, center, dz_raw}
    _abs_meta: dict[int, dict]
    
    def __init__(self, wheel: DevInfo, shifter: DevInfo, ff_passthrough: bool = False, ff_passthrough_easy: bool = False,
                  gear_mapper: Optional[GearMapper] = None,
                  keymap: Optional["KeymapTSV"] = None,
                  keymap_source: str = "both",
                  echo_buttons: bool = False,
                  echo_buttons_tsv: bool = False,
                  args: Optional[argparse.Namespace] = None,
                  mapping_virt2src: Optional[dict] = None,
                  mapping_src2virt: Optional[dict] = None,
                  mapping_mode: str = "priority"):

        # args.mapping_axes / args.mapping_buttons を使ってロード
        try:
            (self.mapping_virt2src,
             self.mapping_src2virt,
             self.map_src2virt_abs,
             self.map_src2virt_key) = build_routing_from_tsv(
                 args.mapping_axes, args.mapping_buttons
             )
            logging.info("[mapping] loaded (axes_groups=%d, buttons_groups=%d)",
                         len(_parse_mapping_tsv(args.mapping_axes) if args.mapping_axes else []),
                         len(_parse_mapping_tsv(args.mapping_buttons) if args.mapping_buttons else []))
        except Exception as e:
            logging.error("[mapping] load failed: %s", e)
            traceback.print_exc()
            self.mapping_virt2src = {}
            self.mapping_src2virt = {}
            self.map_src2virt_abs = {}
            self.map_src2virt_key = {}

        self._ensure_btn_co()

        # --- Button coalesce の初期化（常時作っておく） ---
        def _emit_key(code, val):
            # evdev へキー出力
            self.ui.write(E.EV_KEY, int(code), int(val))
        self._btn_co = _ButtonCoalesce(_emit_key)

        self._axis_scale: dict[int, tuple[int,int,int]] = {}  # code -> (src_min, src_max, mul)
        self._axis_cache = {}          # { ecodes.ABS_*: last_value }
        self._axis_cache_lock = threading.RLock()
        self._last_alive_ts = 0.0
        # ABS_X を中心 0 と決めているなら
        self._axis_cache[ecodes.ABS_X] = 0
        self._axis_cache[ecodes.ABS_Y] = 0
        self._axis_cache[ecodes.ABS_Z] = 0
        self._axis_cache[ecodes.ABS_RZ] = 0

        self._abs_map = {}
        self._abs_owner = {}
        self._abs_meta = {}
        self._abs_src_meta = {}

        # 追加: 実測センターを保持（初期は mid）
        self._abs_src_center = {}   # (role, code) -> int

        self.wheel_info = wheel
        self.shifter_info = shifter
        self.ff_passthrough = ff_passthrough
        self.ff_passthrough_easy = ff_passthrough_easy
        if ff_passthrough_easy:
            self.ff_passthrough = True
        # 文字列想定
        if args.ignore_ffb:
            self.ignore_ffb = args.ignore_ffb
        else:
            self.ignore_ffb = "0"

        # どの仮想ABSコードを誰がオーナーかを覚える
        # 例: { E.ABS_X: "wheel", E.ABS_Y: "wheel", E.ABS_RX: "tanto", ... }
        self._abs_owner: dict[int, str] = {}

        #print("以下FFB無視")
        #print(self.ignore_ffb)
        
        # for FFB Upload
        self.ff_worker: Optional["AsyncFFBProxy"] = None
        self.ff_cache_sig: Dict[Tuple[str, int], int] = {}  # (type, hash)->effect_id
        # for FFB EnQueue
        # self.ff_queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=1024)
        self._ff_consumer_task: asyncio.Task | None = None
        # （任意）同種イベントの間引き用
        self._ff_coalesce: dict[tuple[str, int], int] = {}
        
        self.gear_mapper = gear_mapper

        self.keymap = keymap
        self.keymap_source = keymap_source  # "wheel" | "shift" | "both"
        # マッピング（TSV）
        self.mapping_virt2src = mapping_virt2src or {}
        self.mapping_src2virt = mapping_src2virt or {}
        self.mapping_mode = mapping_mode
        if self.mapping_virt2src or self.mapping_src2virt:
            def _emit_btn(vcode: int, pressed: int):
                self.ui.write(ecodes.EV_KEY, vcode, pressed); self.ui.syn()
            def _emit_abs(vcode: int, value: int):
                self.ui.write(ecodes.EV_ABS, vcode, value); self.ui.syn()
            self._btn_co = _ButtonCoalesce(_emit_btn)
            self._hat_co = _HatCoalesce(_emit_abs, self.mapping_virt2src, mode=("last" if mapping_mode=="last" else "priority"))
        else:
            self._btn_co = None
            self._hat_co = None

        self.echo_buttons = echo_buttons
        self.echo_buttons_tsv = echo_buttons_tsv
        # HAT の現在状態 (-1/0/1) を保持（押下/解放の遷移検出用）
        self._hat_state = {}  # {(src_tag, code): int}
        
        self._effect_types = {}  # id -> ecodes.FF_*
        self.ui_base_fd = None
        self.ui_event_fd = None
        self.ff_mapper = None
        
        self.DEBUG_TELEMETORY = False
        if args.verbose >= 3:
            self.DEBUG_TELEMETORY = True
        
        force_keys = []
        if self.gear_mapper:
            force_keys = GearMapper.STD_GEAR_CODES + [GearMapper.STD_NEUTRAL]

        ui_caps, _ = merge_capabilities(
            wheel.dev, shifter.dev,
            force_keys=force_keys,
            expose_ff=self.ff_passthrough,
            ignore_ffb=self.ignore_ffb,
            ff_off=(args.ff_off if args else False),
            us=self,
        )
        
        # 先に mapper を用意してから渡す
        self.ff_mapper = FfEvioMapper()
        
        # 仮想デバイスの VID/PID/名前を引数で指定可能に
        # （G29偽装が既定：0x046d/0xc24f）
        self.ui = UInputFFDevice(
            ui_caps,
            name=(args.vname if args else "UnderSteer FFB Wheel-Shifter"),
            vid=(args.vid if args else 0x046d),
            pid=(args.pid if args else 0xc24f),
            version=0x0100,
            ff_effects_max=64,
            
            ui_base_fd=self.ui_base_fd,
            ui_base_path="/dev/uinput",
            loop=asyncio.get_running_loop(),
            phys_dev=self.wheel_info.dev,             # ← これがあればベスト
            # phys_event_path="/dev/input/event26"  # 無い場合はこちらで指定
            ff_mapper=self.ff_mapper,
            us=self,
        )
        self.center_all_axes()
        
        def _emit_btn(code, val):
            self.ui.write(E.EV_KEY, int(code), 1 if val else 0)
        self._btn_co = _ButtonCoalesce(_emit_btn)
        
        self.ui_event_path = self.ui.event_path
        print(
            f"[UnderSteer Device] created: {self.ui_event_path} name='{self.ui.name}' "
            f"vid=0x{self.ui.vid:04x} pid=0x{self.ui.pid:04x} "
        )

        print(f"[wheel pys-device]:{self.wheel_info.dev.path}")
        self.wheel_info.hidraw_path = find_hidraw_for_event(self.wheel_info.dev.path)
        if self.wheel_info.hidraw_path:
            logging.info("[wheel] hidraw_path=%s", self.wheel_info.hidraw_path)
        else:
            logging.info("[wheel] hidraw_path= None")
        
        # ここ FFB
        self.phys_fd, self.phys_event_path = open_wheel_event_fd(self.wheel_info, require_ff=True)
        
        logging.info("UnderSteer: InputDevice : %s", self.ui_event_path)
        # 自身（仮想）のイベントを読み取るために open（FF_GAIN/AUTOCENTER 反映用）
        self.self_dev = InputDevice(self.ui_event_path)
        try:
            # 通常は 64 で十分。ホイールによっては 96/128 のこともある
            stats = erase_all_ff_effects_async(self.wheel_info.dev, max_id=64, timeout_per_id=0.50)
            # 必要ならログ出力は stats を見る
        except Exception as e:
            logging.error(f"Pys / erase_all_ff_effects_async failed: {e}")
        
        # 仮想FFBデバイス作成直後に初期ゲイン設定
        # self.wheel_info.dev.fd に対して、Gain/AutoCenter
        try:
            set_initial_ff_gain(self.wheel_info.dev.fd, 75)  # 75%などお好みで
        except Exception as e:
            logging.warning(f"Failed to set initial FFB gain: {e}")
            traceback.print_exc()
        
        # 物理ホイールが何をサポートしているか
        # FF_CONSTANT, FF_SPRING, FF_DAMPER, FF_RUMBLE, FF_PERIODIC, 
        # FF_GAIN, FF_AUTOCENTER 等
        print("# 物理ホイールが何をサポートしているか")
        strW = ""
        for i in self.wheel_info.dev.capabilities(verbose=False).get(ecodes.EV_FF, []):
            strW = strW + " " + FfEvioMapper._ff_type_name(i)
        print(strW)
        
        print("# 仮想ホイールが何をサポートしているか")
        strW = ""
        for i in self.self_dev.capabilities(verbose=False).get(ecodes.EV_FF, []):
            strW = strW + " " + FfEvioMapper._ff_type_name(i)
        print(strW)

        logging.info("UnderSteer: Init End")
        logging.info("---")
        logging.info("")

    def _ensure_btn_co(self):
        if getattr(self, "_btn_co", None) is None:
            from evdev import ecodes as E
            def _emit_btn(code, val):
                try:
                    self.ui.write(E.EV_KEY, int(code), 1 if val else 0)
                except Exception:
                    logging.exception("[btn_co] emit failed (code=%r val=%r)", code, val)
            self._btn_co = _ButtonCoalesce(_emit_btn)

    def _handle_key_event(self, src_tag, ev, keymap=None, gear_mapper=None):
        """
        EV_KEY を TSV マッピングで仮想ボタンへ写像し、_ButtonCoalesce で OR 合流。
        Keymap/GearMapper が先に消費した場合は何もしない。
        """
        from evdev import ecodes as E

        kcode = int(ev.code)
        down  = (int(ev.value) != 0)

        # 1) 既存の Keymap / GearMapper が食うなら優先
        try:
            if keymap and keymap.handle_event(src_tag, ev):
                return
        except Exception:
            pass
        try:
            if gear_mapper and gear_mapper.on_key(src_tag, ev):
                return
        except Exception:
            pass

        # 2) TSV マッピング：物理 (KEY, code) -> 仮想 vcode（多対一 OK）
        vcode = None
        try:
            if hasattr(self, "map_src2virt_key") and self.map_src2virt_key:
                vcode = self.map_src2virt_key.get(("KEY", kcode))
        except Exception:
            vcode = None

        if vcode is not None:
            # OR 合流：どれか1つでも押下があれば1、全て離れれば0
            # src 区別のため (src_tag, 物理コード) を材料にするが、_ButtonCoalesce 側では参照しない
            self._ensure_btn_co()
            self._btn_co.on(int(vcode), (str(src_tag), kcode), bool(down))
            self.ui.syn()
            return

        # 3) フォールバック：マッピングなし → そのまま通す
        self.ui.write(E.EV_KEY, kcode, 1 if down else 0)
        self.ui.syn()

    def center_all_axes(self):
        abs_caps = self.ui.ui_caps.get(ecodes.EV_ABS, [])
        #print(abs_caps)
        # self.ui: evdev.UInput / self._abs_caps: {code: AbsInfo}
        for code, info in abs_caps:
            # AbsInfo(min, max, ...) からセンター計算 (-32767..32767 → 0、-1..1 → 0)
            try:
                c = (int(info.min) + int(info.max)) // 2
            except Exception:
                c = 0
            self.ui.write(E.EV_ABS, code, c)
        self.ui.syn()

    @staticmethod
    def _hat_dir_name(code: int, value: int) -> Optional[str]:
        """ABS_HAT* のコードと値から方向名を返す。value は -1/0/1。"""
        # ABS_HATnX → LEFT/RIGHT, ABS_HATnY → UP/DOWN
        hat_names = {
            ecodes.ABS_HAT0X: ("HAT0_LEFT", "HAT0_RIGHT"),
            ecodes.ABS_HAT0Y: ("HAT0_UP",   "HAT0_DOWN"),
            getattr(ecodes, "ABS_HAT1X", None): ("HAT1_LEFT", "HAT1_RIGHT") if hasattr(ecodes, "ABS_HAT1X") else None,
            getattr(ecodes, "ABS_HAT1Y", None): ("HAT1_UP",   "HAT1_DOWN") if hasattr(ecodes, "ABS_HAT1Y") else None,
        }
        pair = hat_names.get(code)
        if not pair:
            return None
        if value == -1:
            return pair[0]
        if value == 1:
            return pair[1]
        return None

    async def _pipe_events(self, src: InputDevice, src_tag: str):
        """
        [LoopStart] async: src.async_read_loop() : wheel
        [LoopStart] async: src.async_read_loop() : shift
        """
        logging.debug(f"UnderSteer:_pipe_events loop init (%s)", src_tag)

        import evdev
        telem = RateLimitedLogger(min_interval_ms=5000, min_delta=100)
        latest = defaultdict(int)
        
        # 可能なら初期値を一度読む
        try:
            absinfo = src.absinfo
            for name, code in ABS.items():
                if code in absinfo:
                    latest[name] = absinfo[code].value
        except Exception:
            pass

        try:
            print(f"[LoopStart(Rd] : <{src_tag}>")
            async for ev in src.async_read_loop():
                hat_name = None
                # HAT 方向名（-1/0/1 の遷移を押下/解放として管理）
                if ev.type == ecodes.EV_ABS:
                    if ev.code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y, ecodes.ABS_HAT1X, ecodes.ABS_HAT1Y):
                        hat_name = self._hat_dir_name(ev.code, int(ev.value))
                
                # For Logging
                if ev.type == evdev.ecodes.EV_ABS:
                    # 最新値の更新
                    for name, code in ABS.items():
                        if ev.code == code:
                            latest[name] = ev.value
                            break

                if self.DEBUG_TELEMETORY:
                    # ★ 定期/変化時テレメトリ出力（軽量）
                    snapshot = {
                        "steer": latest.get("steer", 0),
                        "thr":   latest.get("throttle", 0),
                        "brk":   latest.get("brake", 0),
                        "clt":   latest.get("clutch", 0),
                        # ついでに内部状態を少し：キュー長やFFスロット利用状況など
                        "q": getattr(self, "_ev_queue_size", 0),
                        "ff_used": getattr(self, "_phys_ff_used", -1),   # 任意: 実装に合わせて更新
                        "ff_cap": getattr(self, "_phys_ff_cap", -1),
                    }
                    if telem.should_emit(snapshot):
                        logging.debug(
                            "[TEL] steer=%6d thr=%5d brk=%5d clt=%5d",
                            snapshot["steer"], snapshot["thr"], snapshot["brk"], snapshot["clt"],
                        )

                # 押したボタン名のエコー（TSV作成補助）
                if self.echo_buttons and ev.type == ecodes.EV_KEY and ev.value == 1:
                    name = code_to_name(ev.code)
                    print(f"[tap][{src_tag}] {name} ({ev.code})", flush=True)
                    if self.echo_buttons_tsv:
                        # そのまま keymap の素材にできるようタブ区切りテンプレ行も出す
                        print(f"{name}\tKEY_???", flush=True)

                # HAT 方向名（-1/0/1 の遷移を押下/解放）
                if ev.type == ecodes.EV_ABS:
                    if ev.code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y) or \
                      hasattr(ecodes, "ABS_HAT1X") and ev.code in (ecodes.ABS_HAT1X, ecodes.ABS_HAT1Y):
                        # ニュートラルの時にしか、HATのキーボード「a,w,s,d」を送らない
                        if GearMapper.neutralFlg:
                            key = (src_tag, ev.code)
                            prev = self._hat_state.get(key, 0)
                            cur = int(ev.value)
                            # 例: 0→1 で RIGHT 押下, 1→0 で RIGHT 解放, -1→1 は LEFT解放→RIGHT押下
                            # まず前の方向を解放
                            if prev != 0:
                                prev_name = self._hat_dir_name(ev.code, prev)
                                if prev_name:
                                    if self.echo_buttons:
                                        print(f"[tap][{src_tag}] {prev_name} (release)", flush=True)
                                    if self.echo_buttons_tsv:
                                        print(f"{prev_name}\tKEY_???", flush=True)
                                    if self.keymap and prev_name in self.keymap.watch_names:
                                        try:
                                            self.keymap.handle_named(prev_name, False)
                                        except Exception as e:
                                            logging.error(f"[keymap] handle_named(release,{prev_name}) failed: {e}")
                            # 次に新しい方向を押下
                            if cur != 0:
                                cur_name = self._hat_dir_name(ev.code, cur)
                                if cur_name:
                                    if self.echo_buttons:
                                        print(f"[tap][{src_tag}] {cur_name} (press)", flush=True)
                                    if self.echo_buttons_tsv:
                                        print(f"{cur_name}\tKEY_???", flush=True)
                                    if self.keymap and cur_name in self.keymap.watch_names:
                                        try:
                                            self.keymap.handle_named(cur_name, True)
                                        except Exception as e:
                                            logging.error(f"[keymap] handle_named(press,{cur_name}) failed: {e}")
                            self._hat_state[key] = cur

                SendKey = False
                # キーボード送出（TSV）: 対象元（wheel/shift/both）と EV_KEY のみ処理
                if (self.keymap and ev.type == ecodes.EV_KEY and (self.keymap_source == "both" or self.keymap_source == src_tag)):
                    if ev.code in self.keymap.watch_codes:
                        try:
                            self.keymap.handle_src_event(ev.code, ev.value)
                            SendKey = True
                        except Exception as e:
                            logging.error(f"[keymap] handle_src_event failed for code={ev.code}, val={ev.value}: {e}")

                if ev.type == ecodes.EV_KEY:
                    # 【Shift の場合】
                    if src_tag == "shift" and self.gear_mapper:
                        # ギア関連キーであれば吸収 → 標準化出力に置換
                        changed = self.gear_mapper.feed_input_key(ev.code, ev.value)
                        # “ギア定義に含まれるキー”は素通し抑止
                        if ev.code in self.gear_mapper.watch_codes:
                            if changed:
                                self.gear_mapper.emit_to(self.ui)
                            # ここでは元イベントは流さない
                        else:
                            # 【Xbox360 の場合】
                            # 物理(KEY, code) → 仮想 vcode
                            vcode = self.map_src2virt_key.get((src_tag, int(ev.code))) if getattr(self, "map_src2virt_key", None) else None
                            if vcode is not None:
                                #self._btn_co.update(int(vcode), bool(int(ev.value) != 0))  # ← OR合流（押下カウント方式）
                                #self.ui.syn()
                                logging.warning(f"box/src_tag: {src_tag}")
                                logging.warning(f"box/vcode: {vcode}")
                                logging.warning(f"box/ev.value: {ev.value}")
                                vnamecode = VIRTUAL_BUTTONS_ORDER[vcode]
                                self.ui.emit(E.EV_KEY, _resolveKeyCode(vnamecode), ev.value)
                                continue
                            # フォールバック（マップに無ければ従来どおり）
                            self.ui.emit(E.EV_KEY, ev.code, ev.value)

                    else:
                        # 物理(KEY, code) → 仮想 vcode
                        vcode = self.map_src2virt_key.get((src_tag, int(ev.code))) if getattr(self, "map_src2virt_key", None) else None
                        if vcode is not None:
                            #self._btn_co.update(int(vcode), bool(int(ev.value) != 0))  # ← OR合流（押下カウント方式）
                            #self.ui.syn()
                            logging.warning(f"src_tag: {src_tag}")
                            logging.warning(f"ev.code: {ev.code} -> {vcode}")
                            logging.warning(f"ev.value: {ev.value}")
                            vnamecode = VIRTUAL_BUTTONS_ORDER[vcode]
                            logging.warning(f"vnamecode: {vnamecode} ({_resolveKeyCode(vnamecode)})")
                            self.ui.emit(E.EV_KEY, _resolveKeyCode(vnamecode), ev.value)
                            continue
                        # フォールバック（マップに無ければ従来どおり）
                        self.ui.emit(E.EV_KEY, ev.code, ev.value)

                elif ev.type == ecodes.EV_ABS:
                    v = ev.value
                    # Default
                    v_scaled = v
                    #if v in (-1, 0, 1):  # HATなどの軸範囲が -1〜1 の場合
                    #    v *= 32767  （削除：スケーラに任せる）
                    routed = False
                    if self._hat_co and (src_tag, "ABS", int(ev.code)) in self.mapping_src2virt:
                        for vname in self.mapping_src2virt[(src_tag, "ABS", int(ev.code))]:
                            if vname in ("ABS_HAT0X","ABS_HAT0Y"):
                                vcode = getattr(ecodes, vname, None)
                                if isinstance(vcode, int):
                                    self._hat_co.on(vname, vcode, src_tag, int(ev.code), int(v))
                                    routed = True
                    if routed:
                        continue
                    # ① TSVの軸割り当て（map_src2virt_abs）を最優先
                    vabs = None
                    if getattr(self, "map_src2virt_abs", None):
                        vabs = self.map_src2virt_abs.get((src_tag, int(ev.code)))
                        logging.warning(f"v-abs: {vabs}")
                    # ② 無ければ従来の恒等マップにフォールバック
                    if vabs is None:
                        vabs = self._map_src_abs_to_virtual(src_tag, ev.code)
                        logging.warning(f"v-absフォールバック {src_tag}/{ev.code}: {vabs}")
                    # REVERSE 指定があれば反転（axisMappings を参照）
                    try:
                        ent = _findReverseOption(axisMappings, src_tag, ev.code)  # 下で定義
                        logging.debug(ent)
                        logging.debug(ent.get("reverse"))
                        if ent and ent.get("reverse"):
                            
                            try:
                                ai = src.absinfo(int(ev.code))
                                smin, smax = int(ai.min), int(ai.max)
                            except Exception:
                                # フォールバック（必要に応じて環境に合わせて調整）
                                smin, smax = 0, 1023
                            v = invertRawValue(int(v), smin, smax)
                    except Exception:
                        pass

                    # スケール
                    v_scaled = self._scale_abs_to_virtual(src_tag, ev.code, vabs, v)
                    self.ui.emit(E.EV_ABS, vabs, v_scaled)

                elif ev.type == ecodes.EV_FF:
                    # 物理から FF が来るケースは稀だが一応無視
                    pass
                elif ev.type in (ecodes.EV_SYN,):
                    pass
                else:
                    # その他は無視（EV_MSC, EV_REL など）
                    pass
                # Loop 完了したらここに来る
                # 一通り終わったよ的な通知を出す
                #self.ui.syn()
                #time.sleep(LoopWait_sec / 100) #fcntl.ioctl の後、必要
        except asyncio.CancelledError:
            # キャンセルで抜ける
            raise
        except OSError as e:
            import errno
            if e.errno == errno.ENODEV:  # 19: No such device
                logging.warning("Input disconnected: %s (%s)", src_tag, e)
                logging.warning(f"{ev.type}/{ev.code}")
            else:
                logging.exception("read_loop error on %s", src_tag)
        finally:
            try:
                src.close()       # これで read が確実に目を覚まして終わる
            except Exception:
                pass

    def register_abs_mapping_first_win(self, role, caps, deadzone_pct=0.025):
        abs_caps = caps.get(ecodes.EV_ABS, [])
        for code, ai in abs_caps:
            vabs = int(code)
            self._abs_map[(role, int(code))] = vabs

            smin = int(getattr(ai, "min", -32768))
            smax = int(getattr(ai, "max",  32767))
            self._abs_src_meta[(role, vabs)] = {"min": smin, "max": smax}
            # 初期センターは mid（実測で後から馴染ませる）
            self._abs_src_center[(role, vabs)] = (smin + smax) // 2

            if vabs not in self._abs_owner:
                self._abs_owner[vabs] = role

            # 仮想側（先勝ちを基準に固定）
            if vabs not in self._abs_meta:
                vmin, vmax = smin, smax
                vcenter = (vmin + vmax) // 2
                full = max(1, vmax - vmin)
                dz_raw = max(1, int(full * deadzone_pct))
                self._abs_meta[vabs] = {"min": vmin, "max": vmax, "center": vcenter, "dz_raw": dz_raw}
            #print(f"vabs : {vabs}")
            #print(self._abs_meta[vabs])
        """
        abs_caps = caps.get(ecodes.EV_ABS, [])
        for code, absinfo in abs_caps:
             vabs = int(code)  # identity
             self._abs_map[(role, int(code))] = vabs

             # 物理側レンジも記録
             try:
                 smin = int(absinfo.min)
                 smax = int(absinfo.max)
             except Exception:
                 smin, smax = -32768, 32767
             self._abs_src_meta[(role, int(code))] = {"min": smin, "max": smax}

             # 仮想（ターゲット）メタは先勝ち優先で決める
             if vabs not in self._abs_owner:
                 self._abs_owner[vabs] = role
             amin, amax = smin, smax
             center = (amin + amax) // 2
             full = max(1, amax - amin)
             dz_raw = max(1, int(full * deadzone_pct))
             if vabs not in self._abs_meta:
                 self._abs_meta[vabs] = {"min": amin, "max": amax, "center": center, "dz_raw": dz_raw}
        """

    # 低速で実測センターを追従（静止時のみ）
    def _track_center(self, role, code, raw):
        key = (role, int(code))
        meta = self._abs_src_meta.get(key); c = self._abs_src_center.get(key)
        if not meta or c is None: return
        smin, smax = meta["min"], meta["max"]
        dz_src = max(1, int((smax - smin) * 0.025))   # 2.5% 相当
        if abs(int(raw) - c) <= dz_src:
            alpha = 0.02  # 遅めのEMAでドリフトだけ吸収
            self._abs_src_center[key] = int(round((1-alpha)*c + alpha*int(raw)))

    @staticmethod
    def _lin_piecewise(raw, smin, c, smax, dmin, dc, dmax):
        r = int(raw)
        if r >= c:
            denom = max(1, smax - c)
            ratio = (r - c) / denom
            return int(round(dc + ratio * (dmax - dc)))
        else:
            denom = max(1, c - smin)
            ratio = (r - c) / denom  # 負値
            return int(round(dc + ratio * (dc - dmin)))

    def _scale_abs_to_virtual(self, role, src_abs, vabs, raw):
        src = self._abs_src_meta.get((role, int(src_abs)))
        #print(f"vabs : {vabs}")
        dst = self._abs_meta.get(int(vabs))
        if not src or not dst: return int(raw)
        smin, smax = src["min"], src["max"]
        #print(f"smin, smax : {smin}, {smax}")
        c = self._abs_src_center.get((role, int(src_abs)), (smin + smax)//2)
        vmin, vmax, vcenter = dst["min"], dst["max"], dst["center"]
        #print(f"vmin, vmax : {vmin}, {vmax}")
        ret = self._lin_piecewise(raw, smin, c, smax, vmin, vcenter, vmax)
        #print(f"ret : {ret}")
        return ret

    def _map_src_abs_to_virtual(self, src_tag: str, src_abs_code: int) -> Optional[int]:
        """
        物理ABS -> 仮想ABS 変換。
        - 明示マップがあればそれを返す
        - 無ければ “未マップ” として None（破棄）を返す
          （※ identity フォールバックをしたい場合は vabs=src_abs_code を返す）
        """
        key = (src_tag, int(src_abs_code))
        return self._abs_map.get(key, None)

    def _write_ff(self, wheel_dev, t, c, v):
        try:
            t0 = time.monotonic()
            ret1 = wheel_dev.write(t, c, v)
            ret2 = wheel_dev.syn()
            dt_ms = (time.monotonic() - t0) * 1000.0
            logging.debug(f"[ff] mirror {c}={v} -> wheel ({wheel_dev.name}) :{dt_ms:.1f} ms)")
        except Exception as e:
            logging.warning(f"[ff] mirror FAILED: code={c} val={v} err={e}")

    async def run(self):
        logging.debug("async run: UnderSteer Start.")
        grabbed = False
        self._stop_ev = asyncio.Event()   # 明示停止フラグ（他所から set してもOK）
        
        loop = asyncio.get_running_loop()
        setup_signal_handlers(loop, self)
        self._tasks = []  # ここでタスクリストを保持
        
        no_grab = build_argparser().parse_args().no_grab
        # --- 物理入力の grab（失敗しても続行できるようにする） ---
        if not no_grab:
            for dev, tag in ((self.wheel_info.dev, "wheel"), (self.shifter_info.dev, "shifter")):
                try:
                    dev.grab()
                    grabbed = True
                    print(f"grabbed: {tag}")
                except Exception as e:
                    # grab 不可でも実運用では続行したいケースが多い
                    logging.warning("grab failed (%s): %s", tag, e)

        print("")
        print("TaskGroup waiting Loop")
        # 例外が1タスクで起きたら全体を畳む実装（TaskGroup）
        try:
            async with asyncio.TaskGroup() as tg:
                # 入力中継（wheel / shifter）
                t1 = tg.create_task(self._pipe_events(self.wheel_info.dev, "wheel"))
                t2 = tg.create_task(self._pipe_events(self.shifter_info.dev, "shift"))
                self._tasks.extend([t1, t2])
                # 明示停止が来るまで待つ（どれかが例外で落ちれば TaskGroup が伝播して抜ける）
                await self._stop_ev.wait()

        except asyncio.CancelledError:
            # 外部からキャンセルされた場合
            logging.debug("async run: cancelled.")
            raise
        except Exception as e:
            logging.error("async run: unhandled error: %s", e)
            traceback.print_exc()
            # ここで return せず finally に抜けてクリーンアップ
        finally:
            
            # --- 安全に締める ---
            # 1) UI close
            ui = getattr(self, "ui", None)
            if ui:
                try:
                    ui.close()
                except Exception:
                    pass

            # 3) grab 解除
            if grabbed:
                for dev, tag in ((self.wheel_info.dev, "wheel"), (self.shifter_info.dev, "shifter")):
                    try:
                        dev.ungrab()
                        logging.debug("ungrabbed: %s", tag)
                    except Exception:
                        pass

            # 4) ff_worker 停止
            if hasattr(self, "ff_worker") and self.ff_worker:
                try:
                    self.ff_worker.stop()
                except Exception:
                    pass

            logging.debug("async run: UnderSteer End.")

    def find_event_from_hidraw(hidraw_path: str) -> str | None:
        """
        /dev/hidrawN から、対応する /dev/input/eventX を高速・安全に探す。
        固まらないように最大深度を 2 に制限し、symlink を追わない。
        """
        try:
            base = os.path.realpath(f"/sys/class/hidraw/{os.path.basename(hidraw_path)}/device")
            if not os.path.isdir(base):
                return None

            # 探索ルート候補: base と base 直下のディレクトリ（最大深度=2）
            roots = [base]
            with os.scandir(base) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        roots.append(e.path)

            for root in roots:
                input_root = os.path.join(root, "input")
                if not os.path.isdir(input_root):
                    continue

                # input/input*/event* を見る（更に深追いしない）
                with os.scandir(input_root) as it_in:
                    for d in it_in:
                        if not (d.is_dir(follow_symlinks=False) and d.name.startswith("input")):
                            continue
                        inputX = os.path.join(input_root, d.name)
                        with os.scandir(inputX) as it_ev:
                            for ev in it_ev:
                                if ev.is_char_device() and ev.name.startswith("event"):
                                    devnode = f"/dev/input/{ev.name}"
                                    if os.path.exists(devnode):
                                        return devnode
            return None
        except Exception:
            return None

    def _open_ff_target(self):
        """
        FFB は /dev/input/eventX に対して行うため、wheel_info.dev を event 起点で渡す。
        hidraw は任意（ログ用途）。無ければ None で構わない。
        """
        # wheel_info.dev は evdev.InputDevice（/dev/input/eventX）
        dev = self.wheel_info.dev
        if dev is None:
            raise RuntimeError("wheel_info.dev is None (event device not available)")

        # hidraw パスが取れているなら渡す（無くてもOK）
        hidraw_path = getattr(self.wheel_info, "hidraw_path", None) or "(no-hidraw)"

        # 推奨：event_dev を渡す
        return HidrawHandle(hidraw_path=hidraw_path, event_dev=dev)
        # または event_path を渡す
        #return HidrawHandle(hidraw_path=hidraw_path, event_path=dev.path)

    def _translate_ff_event_to_op(self, ev):
        """
        EV_FF 入力イベントを FFBProxy 用の操作(op)に変換する。
        返り値は (kind, payload) のタプル。
          kind: "gain" | "autocenter" | "play" | "stop"
          payload: dict（必要なパラメータを格納）
        未対応/不要イベントは None を返す。
        """
        from evdev import ecodes

        logging.error("ここは使ってない")
        if ev.type != ecodes.EV_FF:
            return None

        code = ev.code      # FF_GAIN(96) / FF_AUTOCENTER(97) / それ以外は effect_id
        value = ev.value    # 0=停止, >0=再生(繰返し回数の意味合いを持つこともある)
        
        # 管理系だけはコードで分岐
        if code == ecodes.FF_GAIN:
            # 生値(0..65535)のまま渡す。スケーリングは送出側で。
            logging.debug("gain")
            return ("gain", {"gain": int(value)})

        if code == ecodes.FF_AUTOCENTER:
            # 生値(0..65535)のまま渡す。0=OFF, >0=ON(強さ)
            logging.debug("autocenter")
            return ("autocenter", {"autocenter": int(value)})

        if self.ff_passthrough_easy:
            return None

        # ここからは「エフェクトの再生/停止」トリガ
        # code は "effect_id"（upload 済みの ID）、value は 0/非0（停止/再生）
        effect_id = int(code)

        if value:
            # 一部ドライバは value を繰返し回数として使うことがあるため温存
            return ("play", {"effect_id": effect_id, "repeat": int(value)})
        else:
            return ("stop", {"effect_id": effect_id})

import fcntl
import ctypes
from evdev import ecodes

# uinput ioctl コード定義
EVIOCRMFF = 0x40044581  # _IOW('E', 0x81, int)

def clear_ff_effects(dev, max_id=64):
    """
    仮想デバイスから登録済みのFFエフェクトを削除して空きスロットを作る。
    """
    cleared = 0
    for eid in range(max_id):
        try:
            #fcntl.ioctl(dev.fd, EVIOCRMFF, struct.pack('i', eid))
            fcntl.ioctl(dev.fd, EVIOCRMFF, eid, False)          # ← これでOK
            logging.debug(f"[FFB] Cleared effect id={eid}")
            cleared += 1
        except OSError:
            # 削除できない（未使用 or invalid）場合はスルー
            pass
    logging.info(f"[FFB] Cleared total {cleared} effect slots")
    return cleared





# ------------------------
# CLI
# ------------------------

def build_argparser():
    p = argparse.ArgumentParser(description="UnderSteer — wheelshifter 統合仮想コントローラ")
    p.add_argument("--list", action="store_true", help="検出した入力デバイスを一覧表示して終了")
    p.add_argument("--scan-names", nargs=2, metavar=("WHEEL_KW", "SHIFTER_KW"),
                   default=("wheel", "shift"), help="自動選定に使う名前のキーワード（既定: wheel / shifter）")
    p.add_argument("--wheel", help="wheel デバイスの event パスを明示指定（例: /dev/input/event21）")
    p.add_argument("--shifter", help="shifter デバイスの event パスを明示指定")
    p.add_argument("--ff-pass-through-easy", action="store_true",
                   help="FF_GAIN / FF_AUTOCENTER を物理 wheel へパススルー")
    p.add_argument("--ff-pass-through", action="store_true",
                   help="FFB Command を物理 wheel へ転送")
    p.add_argument("--ignore-ffb", help="Ignore FFB Effect No.")
    p.add_argument("--no-grab", action="store_true", help="物理デバイスを grab しない")
    p.add_argument("--gear-map", help="ギア定義ファイルのパス（ボタン名一覧）を指定すると標準ギア出力を合成（G1..G8→BTN_0..BTN_7、N→BTN_DEAD）")

    # === マッピングTSV ===
    p.add_argument("--export-mapping", action="store_true",
                   help="検出されたデフォルトの軸/ボタン配線をTSVに出力（行の並べ替え＋空行で合流）")
    p.add_argument("--mapping-axes", default="mapping_axes.tsv",
                   help="軸マッピングTSV（空行でグループ化。上から順に仮想へ割当）")
    p.add_argument("--mapping-buttons", default="mapping_buttons.tsv",
                   help="ボタンマッピングTSV（空行でグループ化。上から順に仮想へ割当）")
    p.add_argument("--mapping-mode", choices=["priority","last"], default="priority",
                   help="HAT合流の挙動: priority=グループ内の上から順 / last=最後に動いたソース")
    p.add_argument("--keymap", help="ボタン→キーストロークのTSVファイル")
    p.add_argument("--keymap-source", choices=["wheel","shift","both"], default="both",
                    help="キーボード送出の対象元（wheel/shift/both）")
    p.add_argument("--echo-buttons", action="store_true",
                   help="押したボタン名をログ出力（TSV作成の補助）")
    p.add_argument("--echo-buttons-tsv", action="store_true",
                   help="押下時に『BTN_xxx\\tKEY_???』のテンプレ行も出力")

    # ▼▼ 追加：仮想デバイスの偽装ID/名称を指定できるように ▼▼
    p.add_argument("--vid", type=lambda s: int(s, 0),
                   default=0x046d, help="仮想デバイスの Vendor ID (例: 0x046d = Logitech)")
    p.add_argument("--pid", type=lambda s: int(s, 0),
                   default=0xc24f, help="仮想デバイスの Product ID (例: 0xc24f = G29 Driving Force Racing Wheel PS3)")
    p.add_argument("--vname", type=str, default="UnderSteer FFB Wheel-Shifter",
                   help="仮想デバイス名 (任意の文字列)")

    p.add_argument("--ff-off", action='store_true',
                    help='(temporary) Disable EV_FF on the virtual device to guarantee game startup')

    p.add_argument("-v", "--verbose", action="count", default=0, help="ログ詳細化（-v, -vv, -vvv）")
    return p


def find_by_path(infos: List[DevInfo], path: str) -> Optional[DevInfo]:
    for i in infos:
        #time.sleep(0.002)  # ★空振り時のCPU張り付き防止（2ms）
        if i.path == path:
            return i
    return None

def list_button_names(devinfo, label):
    try:
        caps_v = devinfo.dev.capabilities(verbose=True)   # 文字ラベル付き
        caps_n = devinfo.dev.capabilities(verbose=False)  # 数値キー

        # --- EV_KEY の値リストを取り出す（verbose=True を優先） ---
        key_entries = None
        # verbose=True: キーが ('EV_KEY', 1) みたいなタプル
        for k, v in caps_v.items():
            #time.sleep(0.002)  # ★空振り時のCPU張り付き防止（2ms）
            if isinstance(k, tuple) and len(k) >= 1 and k[0] == 'EV_KEY':
                key_entries = v
                break
        # フォールバック: verbose=False（キーが数値 ecodes.EV_KEY）
        if key_entries is None and ecodes.EV_KEY in caps_n:
            # 数値版は値が単なるコード配列なので、(desc, code) 形に揃える
            key_entries = [ (None, code) for code in caps_n[ecodes.EV_KEY] ]

        if not key_entries:
            print(f"[i] {label}: (no buttons)")
            return

        names = []
        for entry in key_entries:
            #time.sleep(0.002)  # ★空振り時のCPU張り付き防止（2ms）
            # verbose=True: entry は (desc, code)。desc は 'BTN_...' 文字列 か ('BTN_JOYSTICK','BTN_TRIGGER') のタプル
            if isinstance(entry, tuple) and len(entry) == 2:
                desc, code = entry
                if isinstance(desc, tuple) and desc:
                    # 別名タプルなら末尾（より具体的な方が多い）を採用
                    name = desc[-1]
                elif isinstance(desc, str):
                    name = desc
                else:
                    # verbose=False からの整形時 or desc 不明 → 逆引きで名前化
                    name = ecodes.BTN.get(code) or ecodes.KEY.get(code) or f"KEY_{code}"
                names.append(name)
            else:
                # 想定外形：コードだけ来た場合
                code = entry if isinstance(entry, int) else None
                name = (ecodes.BTN.get(code) or ecodes.KEY.get(code) or f"KEY_{code}") if code is not None else "UNKNOWN"
                names.append(name)

        # 重複除去＆安定ソート
        uniq = sorted(set(names))
        print(f"[i] {label}: buttons={len(uniq)} → {', '.join(uniq)}")
    except Exception as e:
        logging.error(f"[i] {label}: failed to enumerate buttons ({e})")

import signal

# これを main() の最初の方に追加
def setup_signal_handlers(loop, app):
    def handle_sigterm():
        print("[Signal] SIGTERM received -> graceful stop.")
        app._stop_ev.set()
        # TaskGroupのタスクを明示的にキャンセル（任意）
        if hasattr(app, "_tasks"):
            for t in list(app._tasks):
                if not t.done():
                    t.cancel()
        try:
            ### task.cancel()
            app.ui.stop()
        except Exception as e:
            raise
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_sigterm)

async def main():
    args = build_argparser().parse_args()
    if args.no_grab:
        no_grab = True
    else:
        no_grab = False

    verb = args.verbose
    if verb >= 3:
        log_level = logging.DEBUG
        # 後からClass内部でTrace設定
        # DEBUG_TELEMETORY
    elif verb == 2:
        log_level = logging.DEBUG
    elif verb == 1:
        log_level = logging.INFO
    else:
        log_level = logging.ERROR
        #log_level = logging.INFO
    setup_logger(
        level=log_level,
        datefmt="%H:%M:%S",
        to_stderr=True,
        log_file = None
    )
    infos = enumerate_input()

    print("")
    print("UnderSteer 実行開始")
    print("  $ " + " ".join(sys.argv))
    print(args)
    print("")
    
    if "-vv" in sys.argv:
        print("[i] 検出デバイス一覧（ベンダID:プロダクトID / name / phys / uniq）")
        for i in infos:
            print("   ", fmt_info(i))
        print("")
        print("")
    if "-vvv" in sys.argv:
        print("[i] 検出デバイス一覧（ベンダID:プロダクトID / name / phys / uniq）")
        for i in infos:
            print("   ", fmt_info(i))
        print("")
        print("")

    if args.list:
        return 0

    # デバイス選定
    wheel_info = None
    shifter_info = None

    if args.wheel:
        wheel_info = find_by_path(infos, args.wheel)
        if not wheel_info:
            print(f"[!] wheel 指定が見つかりません: {args.wheel}", file=sys.stderr)
            return 2
    else:
        wheel_info = pick_device(infos, args.scan_names[0])

    if args.shifter:
        shifter_info = find_by_path(infos, args.shifter)
        if not shifter_info:
            print(f"[!] shifter 指定が見つかりません: {args.shifter}", file=sys.stderr)
            return 2
    else:
        shifter_info = pick_device(infos, args.scan_names[1])

    if not wheel_info or not shifter_info:
        print("[!] wheel / shifter の自動選定に失敗しました。--list で名前を確認し、--wheel/--shifter で明示指定してください。", file=sys.stderr)
        return 3

    logging.debug(f"wheel  : {fmt_info(wheel_info)}")
    logging.debug(f"shifter: {fmt_info(shifter_info)}")
    
    # ボタン名一覧をログ出力
    list_button_names(wheel_info, "wheel")
    list_button_names(shifter_info, "shifter")
    
    # ラン
    gear_mapper = None
    if args.gear_map:
        gear_mapper = GearMapper(Path(args.gear_map))
        # ログ表示（割当の確認）
        gs = len(gear_mapper.gear_requirements)
        print(f"INFO: [gear] defined={gs} gears; neutral={'explicit' if gear_mapper.neutral_button is not None else 'implicit'}; watch={sorted(gear_mapper.watch_codes)}")
        print("")
    keymap = None
    if args.keymap:
        keymap = KeymapTSV(Path(args.keymap))
        print(f"INFO: [keymap] entries={len(keymap.watch_codes)}")
        print(f"INFO: [keymap] entries={len(keymap.watch_names)}")
    
    # ========== マッピングTSV 入出力/合流実装 ==========
    def _get_js_index_for_event(event_path: str) -> int | None:
        import glob
        try:
            base = os.path.basename(event_path)
            sys_event = f"/sys/class/input/{base}"
            dev_dir = os.path.realpath(os.path.join(sys_event, "device"))
            for js in glob.glob(os.path.join(dev_dir, "../../js*")) + glob.glob(os.path.join(dev_dir, "js*")):
                m = re.search(r"js(\d+)$", js)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        try:
            ev = InputDevice(event_path)
            name = ev.name
            for jsn in glob.glob("/dev/input/js*"):
                j = InputDevice(jsn)
                if j.name == name:
                    m = re.search(r"/js(\d+)$", jsn)
                    if m:
                        return int(m.group(1))
        except Exception:
            pass
        return None

    def _write_map_header(f, kind: str):
        if kind == "axes":
            f.write("# VIRTUAL_AXES_ORDER: " + ", ".join(VIRTUAL_AXES_ORDER) + "\n")
        else:
            f.write("# VIRTUAL_BUTTONS_ORDER: " + ", ".join(VIRTUAL_BUTTONS_ORDER) + "\n")
        f.write("# 並び替えガイド: 上から順 / 空行=グループ区切り（同一仮想へ合流）\n")
        f.write("# 列: js_index\tdevice_name\tsrc_tag\tsrc_type\tsrc_code_name\tsrc_code\tdefault_virtual\n")

    def export_default_mapping(wheel_info, shifter_info, axes_path: str, btns_path: str):
        import csv
        rows_axes, rows_btns = [], []
        def _push(devinfo, tag: str):
            if not devinfo or not getattr(devinfo, "dev", None):
                return
            dev = devinfo.dev
            try:
                caps = dev.capabilities(verbose=True, absinfo=True)
            except Exception:
                caps = dev.capabilities(verbose=True)
            js_index = _get_js_index_for_event(getattr(devinfo, "path", None) or getattr(dev, "fn", None) or devinfo.dev.path)
            name = (getattr(devinfo, "name", None) or getattr(dev, "name", None) or "Unknown")
            for item in caps.get(ecodes.EV_ABS, []):
                code_name = str(item[0]) if isinstance(item, (list, tuple)) else str(item)
                try:
                    code = int(getattr(ecodes, code_name))
                except Exception:
                    code = int(item[0]) if isinstance(item, (list, tuple)) and isinstance(item[0], int) else -1
                rows_axes.append([js_index if js_index is not None else -1, name, tag, "ABS", code_name, code, code_name, "NORMAL"])
            for item in caps.get(ecodes.EV_KEY, []):
                code_name = str(item[0]) if isinstance(item, (list, tuple)) else str(item)
                if not code_name.startswith("BTN_"): 
                    continue
                try:
                    code = int(getattr(ecodes, code_name))
                except Exception:
                    code = -1
                rows_btns.append([js_index if js_index is not None else -1, name, tag, "KEY", code_name, code, code_name])
        _push(wheel_info, "wheel")
        _push(shifter_info, "shift")
        with open(axes_path, "w", encoding="utf-8", newline="") as f:
            _write_map_header(f, "axes")
            w = __import__("csv").writer(f, delimiter="\t")
            for r in rows_axes:
                w.writerow(r); f.write("\n")
        with open(btns_path, "w", encoding="utf-8", newline="") as f:
            _write_map_header(f, "buttons")
            w = __import__("csv").writer(f, delimiter="\t")
            for r in rows_btns:
                w.writerow(r); f.write("\n")
        print(f"[mapping] exported: {axes_path}, {btns_path}")

    def _load_grouped(path: str):
        groups, cur = [], []
        if not os.path.exists(path): return groups
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"): continue
                if not line.strip():
                    if cur: groups.append(cur); cur=[]
                    continue
                cols = line.rstrip("\n").split("\t"); cols += ["" for _ in range(max(0,7-len(cols)))]
                try:
                    cols[0] = int(cols[0]) if str(cols[0]).strip().isdigit() else -1
                    cols[5] = int(cols[5]) if str(cols[5]).strip().isdigit() else -1
                except Exception: pass
                cur.append(cols[:7])
        if cur: groups.append(cur)
        return groups

    mapping_virt2src, mapping_src2virt = {}, {}

    app = UnderSteer(
        wheel_info, shifter_info,
        ff_passthrough=args.ff_pass_through,
        ff_passthrough_easy=args.ff_pass_through_easy,
        gear_mapper=gear_mapper,
        keymap=keymap,
        keymap_source=args.keymap_source,
        echo_buttons=args.echo_buttons,
        echo_buttons_tsv=args.echo_buttons_tsv,
        args=args,  # ← 引数を明示的に渡す！
        mapping_virt2src=mapping_virt2src,
        mapping_src2virt=mapping_src2virt,
        mapping_mode=args.mapping_mode
    )

    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop, app)

    # grab 無効なら掴まない
    if args.no_grab:
        try:
            wheel_info.dev.ungrab()
            print("ungrab wheel")
        except Exception:
            pass
        try:
            shifter_info.dev.ungrab()
            print("ungrab shifter")
        except Exception:
            pass
    
    try:
        await app.run()
    except KeyboardInterrupt:
        print("\n[] Ctrl-C で終了")
    finally:
        try:
            if keymap:
                keymap.close()
        except Exception:
            pass
    return 0

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("[!] 実行には通常 root 権限が必要です（/dev/uinput, /dev/input/event* のアクセス）", file=sys.stderr)
    rc = asyncio.run(main())
    sys.exit(rc)