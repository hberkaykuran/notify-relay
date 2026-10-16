"""Draw icon.ico: two stacked cards, the shape of the relay's peek stack.

Run once when the design changes; icon.ico is committed and bundled.
"""

from PIL import Image, ImageDraw

SIZE = 256


def draw():
    im = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # back card, peeking out above the front one
    d.rounded_rectangle((44, 30, 212, 150), radius=26, fill=(132, 132, 140, 255))
    # front card
    d.rounded_rectangle((20, 74, 236, 226), radius=30, fill=(244, 244, 246, 255))
    # header dot and two text lines
    d.ellipse((50, 102, 80, 132), fill=(124, 92, 214, 255))
    d.rounded_rectangle((94, 108, 200, 126), radius=9, fill=(60, 60, 66, 255))
    d.rounded_rectangle((50, 158, 206, 176), radius=9, fill=(150, 150, 158, 255))
    d.rounded_rectangle((50, 190, 160, 208), radius=9, fill=(150, 150, 158, 255))
    return im


if __name__ == "__main__":
    draw().save("icon.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48),
                                   (64, 64), (128, 128), (256, 256)])
    print("wrote icon.ico")
