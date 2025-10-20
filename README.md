---

## 🏎️ UnderSteer / UnitedWheelShifter

### ― Linux向け Force Feedback Wheel + Shifter 統合ツール ―

(English below)

---

### 🎯 概要

**UnderSteer** は、複数の物理USBデバイス（ハンコン・シフターなど）を
1つの仮想コントローラとして統合し、**Force Feedback（FFB）を完全中継**する
Linux 向けツールです。

ゲームからの FFB 信号を受け取り、自分のソフトと物理デバイスで“半分ずつ”処理する
ことで、**フリーズせず滑らかに反応する FFB** を実現しました。

> 🎮 「半分はゲームソフトの取り分、半分は自分の取り分」
> ── そのバランスで、Project CARS 2 もついに固まらず動作！

---

### 🧩 主な機能

* 🧠 **複数デバイスの統合**

  * Wheel, Shifter, Pedal などを 1台の仮想デバイスへ統合
  * それぞれの軸・ボタンを再マップし、欠けなく統一

* ⚙️ **Force Feedback パススルー**

  * ゲーム → 仮想デバイス → 物理デバイスへ FFB を中継
  * FF_CONSTANT / FF_SPRING / FF_DAMPER / FF_RUMBLE 対応
  * FFB効果をバッファリングして、非同期でもブロックなし！

* 🧾 **高度なログ・トレース**

  * `[Δxx.x ms]` 形式の時差付きカラー出力
  * `EVIOCSFF`, `UI_BEGIN_FF_UPLOAD`, `ERASE` など ioctl 処理を追跡

* 🧯 **スタック防止設計**

  * FFB stall（>2s）を検知して Panic FLUSH
  * Timeout, EAGAIN, ENOSPC などを安全にハンドリング

* 🧠 **自動デバイス認識**

  * “wheel” を含むデバイス → Wheel
  * “shifter” を含むデバイス → Shifter
  * Vendor/Product ID を自動抽出してログ一覧出力

---

### 🪄 開発の要点

| 機能         | 技術的ポイント                                 |
| ---------- | --------------------------------------- |
| 仮想入力生成     | `uinput` + `evdev` + `ctypes.Structure` |
| 非ブロッキングFFB | `select.poll()` + 短周期 `poll(50)`        |
| エフェクト管理    | `UI_BEGIN_FF_UPLOAD / ERASE` ループ監視      |
| メモリリーク対策   | `valgrind` で追跡済み                        |
| FFB負荷分散    | ゲーム:50% / 自ソフト:50%                      |

---

### ⚙️ 動作確認済み環境

| 項目     | 内容                                              |
| ------ | ----------------------------------------------- |
| OS     | Ubuntu 25.04 (Plucky Puffin)                    |
| Kernel | 6.14.x-generic                                  |
| デバイス   | Thrustmaster T128, VelocityOne Shifter          |
| 対応ゲーム  | rFactor2 ✅ / Project CARS 2 ✅ / Assetto Corsa ✅ |
| Proton | GE-Proton10-10                                  |
| Python | 3.13+                                           |



## ⚙️ FFB 動かない時の対策（Dirt Rally など）

### ✅ SDLをevdev強制モードにする

以下の環境変数を指定して Steam / Dirt Rally を起動：

```
SDL_GAMECONTROLLER_IGNORE_DEVICES=0
SDL_HIDAPI_JOYSTICK=0
```

これで HIDAPI を無効化し、`/dev/input/eventX` 経由の EV_FF を使わせます。
 （多くのゲームではこれで有効になります）



---

### 🧠 開発者コメント

> FFBの同期ずれ、ゲームのフリーズ、
> 何度も繰り返した実験の末に辿り着いた「理想の半分」。
> **ゲームのリズムを壊さず、物理機器の魂を活かす。**
>
> ― 田中秀宗（Tanaka Computer Service Corp.）

---

## 🌐 English Section

### 🎯 Overview

**UnderSteer** (also known as *UnitedWheelShifter*) is a Linux tool that merges multiple USB racing devices — such as wheels and shifters — into a **single virtual controller** with full **Force Feedback (FFB) passthrough**.

By balancing FFB processing **half by the game, half by the tool**,
it achieves *smooth and non-blocking feedback*, even in demanding titles like Project CARS 2.

> 🏁 *“Half belongs to the game, half belongs to my software.”*
> — The key to perfect FFB harmony.

---

### 🧩 Features

* **Multi-device merging**
  Combines wheel + shifter + pedal into one virtual HID controller.

* **FFB passthrough engine**
  Handles Constant, Spring, Damper, and Rumble effects safely.
  Fully asynchronous, non-blocking architecture.

* **Rich logging**
  Millisecond delta timestamps with color-coded trace logs.

* **Anti-freeze watchdog**
  Detects >2s FFB stalls and automatically flushes the queue.

* **Auto device detection**
  Detects “wheel” and “shifter” by name, logs all USB device IDs.

---

### 🧱 Technical Highlights

| Area              | Technique                             |
| ----------------- | ------------------------------------- |
| Virtual I/O       | `uinput`, `evdev`, `ctypes.Structure` |
| Non-blocking loop | `select.poll(50)` with short sleeps   |
| Effect sync       | `UI_BEGIN_FF_UPLOAD / ERASE` tracking |
| Leak detection    | `valgrind` verified                   |
| Load balancing    | 50% game / 50% software               |

---

### ✅ Tested Environment

| Component | Specification                             |
| --------- | ----------------------------------------- |
| OS        | Ubuntu 25.04                              |
| Kernel    | 6.14.x                                    |
| Devices   | Thrustmaster T128 / VelocityOne Shifter   |
| Games     | rFactor2 / Project CARS 2 / Assetto Corsa |
| Proton    | GE-Proton10-10                            |
| Python    | 3.13+                                     |



## ⚙️ Solutions for when FFB doesn't work (Dirt Rally, etc.)

### ✅ Force SDL to use EVDEV mode

Launch Steam / Dirt Rally with the following environment variables:

```
SDL_GAMECONTROLLER_IGNORE_DEVICES=0
SDL_HIDAPI_JOYSTICK=0
```

This will disable HIDAPI and force EV_FF via `/dev/input/eventX`.
(This will work for most games.)



---

### 💬 Developer’s Note

> After countless freezes and mismatched timings,
> I found the balance: the game and my driver both alive.
>
> **A shared control between virtual and physical worlds.**

---

### 📦 License

MIT License © 2025 Tanaka Computer Service Corp.

---

### 🧰 Repository Structure

```
understeer/
├── understeer.py         # main integration logic
├── ffb_proxy.py          # async FFB passthrough handler
├── device_mapper.py      # wheel/shifter merge and ID map
├── uinput_wrapper.py     # virtual controller creation
└── logs/                 # structured trace outputs
```

---

### 🚀 Quick Start

```bash
sudo apt install python3-evdev
git clone https://github.com/tanaka-cs/UnderSteer.git
cd UnderSteer
sudo python3 understeer.py
```

Output example:

```
03:14:25.481 [Δ  0.1 ms] [INFO] [FFB-Pys] UI_BEGIN_FF_UPLOAD (virt_id=0)
03:14:25.482 [Δ  0.2 ms] [INFO] [ff物理] SPRING(83) up 送信成功
03:14:25.482 [Δ  0.0 ms] [DEBUG] [TRACE] after EVIOCSFF id=0
```

---

### 🌍 Links

* 🏁 GitHub: [https://github.com/hidemune/understeer](https://github.com/hidemune/understeer)
* 🧠 Developer: [Tanaka Computer Service Corp.](https://tanaka-cs.co.jp)
* 💬 Contact: [hidemune@tanaka-cs.co.jp](mailto:hidemune@tanaka-cs.co.jp)

---

