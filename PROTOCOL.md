# USB serial protocol

This documents the USB wire protocol the app uses to talk to a 6‑colour e‑ink
photo frame, so the encoder and transfer loop in [`usbme_photo_cast.py`](usbme_photo_cast.py)
can be understood or re‑implemented. It was determined by observing the frame's
serial traffic and confirmed on real hardware.

## Physical link

- The frame enumerates as a **CH340 USB‑serial** adapter (`VID 0x1A86`, `PID 0x7523`).
- **115200 baud, 8 data bits, no parity, 1 stop bit** (8N1), no flow control.
- The app auto‑selects the first port whose VID/PID matches; otherwise the first
  serial port found.

## Frame format

Every packet — in both directions — has the same shape:

```
AC 5A  <cmd>  <len_hi> <len_lo>  <payload…>  <xor>
```

| Field       | Size | Notes                                                        |
|-------------|------|--------------------------------------------------------------|
| `AC 5A`     | 2    | Fixed start‑of‑frame marker                                   |
| `cmd`       | 1    | Opcode (see below)                                           |
| `len`       | 2    | Payload length, big‑endian (`len_hi`, `len_lo`)             |
| `payload`   | len  | Command‑specific bytes                                       |
| `xor`       | 1    | XOR of **every preceding byte** in the packet (from `AC`)   |

Replies use the same framing; their payloads are short ASCII strings. The app
matches on a substring token (e.g. `OK`, `EARSE_OK`, `<n>_OK`) rather than parsing
the whole reply, because acknowledgements sometimes arrive coalesced in one read.

## Opcodes

| Cmd    | Meaning              | Payload sent                     | Reply contains          |
|--------|----------------------|----------------------------------|-------------------------|
| `0x23` | Get serial / version | `"SN"`                           | `SN:<serial>;VR:<fw>;`  |
| `0x25` | Get battery          | *(empty)*                        | `V:<millivolts>;`       |
| `0x29` | Get provisioning mode| *(empty)*                        | `NET:<0\|1>;` (1 = provisioning) |
| `0x14` | Set Wi‑Fi            | `K:<ssid>;V:<password>;P:<phone>;` | `KN_OK`               |
| `0x26` | File header (+erase) | see below                        | `OK`, then `EARSE_OK`   |
| `0x27` | File chunk           | see below                        | `<index>_OK`            |

> The app only ever queries identity/battery/mode, sets Wi‑Fi, and pushes an
> image. It never writes the serial number.

### `0x26` — file header

Begins an image transfer and triggers the panel erase. The payload is exactly
5 bytes, so the length field is `00 05`:

```
AC 5A 26 00 05  <size:4 big-endian>  <sum:1>  <xor>
```

- `size` — total number of image bytes about to be sent.
- `sum`  — `(sum of all image bytes) & 0xFF`.

The frame replies `OK` to accept the header, then `EARSE_OK` once the e‑ink erase
completes (a couple of seconds later). **Timing gotcha:** `OK` and `EARSE_OK`
sometimes arrive in a single serial read and sometimes separately — after seeing
`OK`, check whether `EARSE_OK` is already in the buffer before waiting again.

### `0x27` — file chunk

The image is sent in **1024‑byte** chunks, each tagged with a 16‑bit index. The
length field is the chunk length + 2 (for the index):

```
AC 5A 27  <len:2 = chunklen+2>  <index:2 big-endian>  <chunk…>  <xor>
```

The frame acknowledges each chunk with `"<index>_OK"` (e.g. `0_OK`, `1_OK`, …).
After the last declared byte arrives, the frame refreshes the display.

## Transfer sequence

```
0x23  "SN"                 → SN:…;VR:…;      (alive check)
0x26  size + checksum      → OK → EARSE_OK   (header + erase)
0x27  chunk[0]             → 0_OK
0x27  chunk[1]             → 1_OK
…
0x27  chunk[n-1]           → (n-1)_OK        (frame then refreshes)
```

## Image format

The panel is a 6‑colour e‑ink display. An image is prepared like this:

1. Resize/crop to the panel resolution.
2. Floyd–Steinberg dither to the 6‑colour palette
   **black, white, red, green, blue, yellow**.
3. Map each palette colour to the frame's 4‑bit code:

   | Colour | black | white | yellow | red | blue | green |
   |--------|:-----:|:-----:|:------:|:---:|:----:|:-----:|
   | Code   | 0     | 1     | 2      | 3   | 5    | 6     |

4. Pack **two pixels per byte** (4 bits per pixel); the first (left) pixel goes
   in the high nibble.

The resulting `.bin` is therefore `(width / 2) * height` bytes.

## Panel resolutions

The model is read from the serial‑number prefix, which selects the panel size:

| Model  | SN prefix | Panel (W×H) | `.bin` size |
|--------|-----------|-------------|-------------|
| F7 Pro | `F7PRO`   | 1024 × 600  | 307,200 B   |
| F7     | `F7`      | 800 × 480   | 192,000 B   |
| F13    | `F13`     | 1200 × 1600 | 960,000 B   |
| F8     | `F8`      | 1200 × 1600 | 960,000 B   |

Sending a `.bin` whose size doesn't match the panel makes the frame reject the
header (`FAIL`). Resolutions other than F7 Pro are wired up but have only been
verified on an F7 Pro.
