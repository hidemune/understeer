~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
🏎️ UnderSteer — Unified Wheel & Shifter Integration Tool
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

UnderSteer is a Linux utility that automatically detects multiple 
physical devices (steering wheels and shifters) and integrates them 
into a single virtual controller.

Key Features:

Automatically scans for "wheel" and "shift" in the device name and 
pairs each device.

Integrates axis and button inputs from both devices and sends them 
to the virtual controller.

Transfers force feedback (FFB) from the game to the physical wheel.

Userspace implementation using python-evdev + uinput.

This tool supports generic HID devices from Thrustmaster, VelocityOne, 
Logitech, and others, providing a natural control experience 
in racing simulators such as Forza Horizon, Assetto Corsa, and rFactor.



◆◆◆ What is this tool?

This was created with the goal of combining two USB controllers, 
the "Thrustmaster Racing Wheel FFB" and
the "VelocityOne Multi-Shift," and having them recognized 
as a single steering wheel controller.

It also supports force feedback.



◆◆◆ How this tool works

1. Search for USB devices using the wheel and shift strings and integrate them 
into one device.

[i] wheel  : /dev/input/event26 | --:-- | name='Thrustmaster Thrustmaster 
Racing Wheel FFB' phys='usb-0000:02:00.0-1.3/input0' uniq=''
[i] shifter: /dev/input/event27 | --:-- | name='VelocityOne Multi-Shift 
VelocityOne Multi-Shift' phys='usb-0000:02:00.0-1.2.1/input0' 
uniq='TBRS004-20241224'


2. A controller named "UnderSteer FFB Wheel-Shifter" will be created.
We've disguised it as a Logitech G29 in the source code to make it easier 
for many games to recognize.
Since it's hard-coded, you can change it by rewriting the source if necessary.

            name="UnderSteer FFB Wheel-Shifter",
            vendor=0x046d,   # ← Logitech, Inc.
            product=0xc24f,  # ← G29 Driving Force Racing Wheel (PS3)
            version=0x0100,  # 


3. In-game, it will be recognized as a "Logitech G29 Racing Wheel."
The external H-shifter will be recognized and integrated as a single device.
Force feedback will also be enabled.



⚙️ Steam launch options [Reference]

・Forza Horizon 5 - Steam launch options

SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT=0x046d/0xc24f SDL_HIDAPI_IGNORE_DEVICES_EXCEPT=0x046d/0xc24f %command%

In Forza Horizon 5, only one device could be read,
so I was able to force it to be recognized using the above launch option.
This is a Steam launch command that instructs it to ignore anything other 
than the "Logitech G29 Racing Wheel."


・Forza Horizon 6

https://youtu.be/lwlw0ChiSR0

Apparently Forza Horizon 6 is coming out. I'm looking forward to it.


⚙️ gears.dat [Reference]

BTN_0
BTN_1
BTN_2
BTN_3
BTN_4
BTN_5
BTN_6
BTN_7

These are the button names for 1st to 8th gear on my 8-speed shifter.
If you set the activation options correctly,
you can make it so that you can hold down the neutral button while the gear 
is in neutral. (Use BTN_DEAD)

I think I used it at Test Drive Unlimited Soler Crown.



⚙️ Current help information [Reference]

$ sudo python3 understeer.py --help
usage: understeer.py [-h] [--list] [--scan-names WHEEL_KW SHIFTER_KW]
                     [--wheel WHEEL] [--shifter SHIFTER]
                     [--ff-pass-through-easy] [--ff-pass-through] [--no-grab]
                     [--gear-map GEAR_MAP] [--keymap KEYMAP]
                     [--keymap-source {wheel,shift,both}] [--echo-buttons]
                     [--echo-buttons-tsv] [--vid VID] [--pid PID]
                     [--vname VNAME] [--ff-off] [-v]



UnderSteer — wheelshifter integrated virtual controller

options:
  -h, --help            show this help message and exit
  --list                List detected input devices and exit
  --scan-names WHEEL_KW SHIFTER_KW
                        Name keyword to use for autoselection 
                        (default: wheel / shifter)
  --wheel WHEEL         Explicitly specify the event path of the wheel device 
                        (e.g. /dev/input/event21)
  --shifter SHIFTER     Explicitly specify the event path for the shifter device
  --ff-pass-through-easy
                        Pass through FF_GAIN / FF_AUTOCENTER to physical wheels
  --ff-pass-through     Transferring FFB Commands to Physics Wheels
  --no-grab             Do not grab physical devices (beware of collisions)
  --gear-map GEAR_MAP   Specify the path of the gear definition file 
                        (button name list) to synthesize the standard 
                        gear output (G1..G8 → BTN_0..BTN_7, N → BTN_DEAD)
  --keymap KEYMAP       Button → Keystroke TSV file
  --keymap-source {wheel,shift,both}
                            Keyboard send source (wheel/shift/both)
  --echo-buttons        Log the name of the pressed button 
                        (assistance for creating TSV)
  --echo-buttons-tsv    When pressed, the template line 
                        "BTN_xxx\tKEY_???" is also output
  --vid VID             Virtual device Vendor ID (e.g. 0x046d = Logitech)
  --pid PID             Virtual device product ID 
                        (e.g., 0xc24f = G29 Driving Force Racing Wheel PS3)
  --vname VNAME         Virtual device name (any string)
  --ff-off              (temporary) Disable EV_FF on the virtual device 
                        to guarantee game startup
  -v, --verbose         Log verbosity (-v, -vv)




[Change history]

2025/10/16  Ver 1.31 : Debug

2025/10/16  Ver 1.3 : Expand FFB

OK★　: FF_GAIN
OK★　: FF_AUTOCENTER

OK★　: FF_CONSTANT
OK★　: FF_SPRING
OK★　: FF_DAMPER
OK★　: FF_RUMBLE

: FF_PERIODIC
: FF_FRICTION
: FF_INERTIA
: FF_RAMP
: FF_SQUARE       (periodic subtype)
: FF_TRIANGLE     (periodic subtype)
: FF_SINE         (periodic subtype)
: FF_SAW_UP       (periodic subtype)
: FF_SAW_DOWN     (periodic subtype)

2025/10/09  Ver 1.1 : Correct FFB

2025/10/08  Ver 1.0 : Initial Commit


[Created by]

田中　秀宗 / Hidemune TANAKA
