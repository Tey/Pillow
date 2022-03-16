#
# The Python Imaging Library.
# $Id$
#
# PPM support for PIL
#
# History:
#       96-03-24 fl     Created
#       98-03-06 fl     Write RGBA images (as RGB, that is)
#
# Copyright (c) Secret Labs AB 1997-98.
# Copyright (c) Fredrik Lundh 1996.
#
# See the README file for information on usage and redistribution.
#


from . import Image, ImageFile

#
# --------------------------------------------------------------------

b_whitespace = b"\x20\x09\x0a\x0b\x0c\x0d"

MODES = {
    # standard
    b"P1": "1",
    b"P2": "L",
    b"P3": "RGB",
    b"P4": "1",
    b"P5": "L",
    b"P6": "RGB",
    # extensions
    b"P0CMYK": "CMYK",
    # PIL extensions (for test purposes only)
    b"PyP": "P",
    b"PyRGBA": "RGBA",
    b"PyCMYK": "CMYK",
}


def _accept(prefix):
    return prefix[0:1] == b"P" and prefix[1] in b"0123456y"


##
# Image plugin for PBM, PGM, and PPM images.


class PpmImageFile(ImageFile.ImageFile):

    format = "PPM"
    format_description = "Pbmplus image"

    def _read_magic(self):
        magic = b""
        # read until whitespace or longest available magic number
        for _ in range(6):
            c = self.fp.read(1)
            if not c or c in b_whitespace:
                break
            magic += c
        return magic

    def _read_token(self):
        token = b""
        while len(token) <= 10:  # read until next whitespace or limit of 10 characters
            c = self.fp.read(1)
            if not c:
                break
            elif c in b_whitespace:  # token ended
                if not token:
                    # skip whitespace at start
                    continue
                break
            elif c == b"#":
                # ignores rest of the line; stops at CR, LF or EOF
                while self.fp.read(1) not in b"\r\n":
                    pass
                continue
            token += c
        if not token:
            # Token was not even 1 byte
            raise ValueError("Reached EOF while reading header")
        elif len(token) > 10:
            raise ValueError(f"Token too long in file header: {token}")
        return token

    def _open(self):
        magic_number = self._read_magic()
        try:
            mode = MODES[magic_number]
        except KeyError:
            raise SyntaxError("not a PPM file")

        if magic_number in (b"P1", b"P4"):
            self.custom_mimetype = "image/x-portable-bitmap"
        elif magic_number in (b"P2", b"P5"):
            self.custom_mimetype = "image/x-portable-graymap"
        elif magic_number in (b"P3", b"P6"):
            self.custom_mimetype = "image/x-portable-pixmap"

        for ix in range(3):
            token = int(self._read_token())
            if ix == 0:  # token is the x size
                xsize = token
            elif ix == 1:  # token is the y size
                ysize = token
                if mode == "1":
                    self.mode = "1"
                    rawmode = "1;I"
                    break
                else:
                    self.mode = rawmode = mode
            elif ix == 2:  # token is maxval
                maxval = token
                if maxval > 255:
                    if not mode == "L":
                        raise ValueError(f"Too many colors for band: {maxval}")
                    if maxval < 2**16:
                        self.mode = "I"
                        rawmode = "I;16B"
                    else:
                        self.mode = "I"
                        rawmode = "I;32B"

        decoder_name = "raw"
        if magic_number in (b"P1", b"P2", b"P3"):
            decoder_name = "ppm_plain"
        self._size = xsize, ysize
        self.tile = [
            (decoder_name, (0, 0, xsize, ysize), self.fp.tell(), (rawmode, 0, 1))
        ]


#
# --------------------------------------------------------------------


class PpmPlainDecoder(ImageFile.PyDecoder):
    _pulls_fd = True

    def _read_block(self):
        return self.fd.read(ImageFile.SAFEBLOCK)

    def _find_comment_end(self, block, start=0):
        a = block.find(b"\n", start)
        b = block.find(b"\r", start)
        return min(a, b) if a * b > 0 else max(a, b)  # lowest nonnegative index (or -1)

    def _ignore_comments(self, block):
        """
        Deletes comments from block.
        If comment does not end in this block, raises a flag.
        """
        comment_spans = False
        while True:
            comment_start = block.find(b"#")  # look for next comment
            if comment_start == -1:  # no comment found
                break
            comment_end = self._find_comment_end(block, comment_start)
            if comment_end != -1:  # comment ends in this block
                # delete comment
                block = block[:comment_start] + block[comment_end + 1 :]
            else:  # last comment continues to next block(s)
                block = block[:comment_start]
                comment_spans = True
                break
        return block, comment_spans

    def _decode_bitonal(self):
        """
        This is a separate method because the plain PBM format all data tokens
        are exactly one byte, and so the inter-token whitespace is optional.
        """
        decoded_data = bytearray()
        total_bytes = self.state.xsize * self.state.ysize

        comment_spans = False
        while len(decoded_data) != total_bytes:
            block = self._read_block()  # read next block
            if not block:
                # eof
                break

            while block and comment_spans:
                comment_end = self._find_comment_end(block)
                if comment_end != -1:  # comment ends in this block
                    block = block[comment_end + 1 :]  # delete tail of previous comment
                    comment_spans = False
                else:  # comment spans whole block
                    block = self._read_block()

            block, comment_spans = self._ignore_comments(block)

            tokens = b"".join(block.split())
            for token in tokens:
                if token not in (48, 49):
                    raise ValueError(f"Invalid token for this mode: {bytes([token])}")
            decoded_data = (decoded_data + tokens)[:total_bytes]
        invert = bytes.maketrans(b"01", b"\xFF\x00")
        return decoded_data.translate(invert)

    def _decode_blocks(self, channels=1, depth=8):
        decoded_data = bytearray()
        # HACK: 32-bit grayscale uses signed int
        maxval = 2 ** (31 if depth == 32 else depth) - 1
        max_len = 10
        bytes_per_sample = depth // 8
        total_bytes = self.state.xsize * self.state.ysize * channels * bytes_per_sample

        comment_spans = False
        half_token = False
        while len(decoded_data) != total_bytes:
            block = self._read_block()  # read next block
            if not block:
                if half_token:
                    block = bytearray(b" ")  # flush half_token
                else:
                    # eof
                    break

            while block and comment_spans:
                comment_end = self._find_comment_end(block)
                if comment_end != -1:  # comment ends in this block
                    block = block[comment_end + 1 :]  # delete tail of previous comment
                    break
                else:  # comment spans whole block
                    block = self._read_block()

            block, comment_spans = self._ignore_comments(block)

            if half_token:
                block = half_token + block  # stitch half_token to new block

            tokens = block.split()

            if block and not block[-1:].isspace():  # block might split token
                half_token = tokens.pop()  # save half token for later
                if len(half_token) > max_len:  # prevent buildup of half_token
                    raise ValueError(
                        f"Token too long found in data: {half_token[:max_len + 1]}"
                    )

            for token in tokens:
                if len(token) > max_len:
                    raise ValueError(
                        f"Token too long found in data: {token[:max_len + 1]}"
                    )
                token = int(token)
                if token > maxval:
                    raise ValueError(f"Channel value too large for this mode: {token}")
                decoded_data += token.to_bytes(bytes_per_sample, "big")
                if len(decoded_data) == total_bytes:  # finished!
                    break
        return decoded_data

    def decode(self, buffer):
        rawmode = self.args[0]

        if self.mode == "1":
            decoded_data = self._decode_bitonal()
            rawmode = "1;8"
        elif self.mode == "L":
            decoded_data = self._decode_blocks(channels=1, depth=8)
        elif self.mode == "I":
            if rawmode == "I;16B":
                decoded_data = self._decode_blocks(channels=1, depth=16)
            elif rawmode == "I;32B":
                decoded_data = self._decode_blocks(channels=1, depth=32)
        elif self.mode == "RGB":
            decoded_data = self._decode_blocks(channels=3, depth=8)

        self.set_as_raw(bytes(decoded_data), rawmode)
        return -1, 0


#
# --------------------------------------------------------------------


def _save(im, fp, filename):
    if im.mode == "1":
        rawmode, head = "1;I", b"P4"
    elif im.mode == "L":
        rawmode, head = "L", b"P5"
    elif im.mode == "I":
        if im.getextrema()[1] < 2**16:
            rawmode, head = "I;16B", b"P5"
        else:
            rawmode, head = "I;32B", b"P5"
    elif im.mode == "RGB":
        rawmode, head = "RGB", b"P6"
    elif im.mode == "RGBA":
        rawmode, head = "RGB", b"P6"
    else:
        raise OSError(f"cannot write mode {im.mode} as PPM")
    fp.write(head + ("\n%d %d\n" % im.size).encode("ascii"))
    if head == b"P6":
        fp.write(b"255\n")
    if head == b"P5":
        if rawmode == "L":
            fp.write(b"255\n")
        elif rawmode == "I;16B":
            fp.write(b"65535\n")
        elif rawmode == "I;32B":
            fp.write(b"2147483648\n")
    ImageFile._save(im, fp, [("raw", (0, 0) + im.size, 0, (rawmode, 0, 1))])

    # ALTERNATIVE: save via builtin debug function
    # im._dump(filename)


#
# --------------------------------------------------------------------

Image.register_decoder("ppm_plain", PpmPlainDecoder)
Image.register_open(PpmImageFile.format, PpmImageFile, _accept)
Image.register_save(PpmImageFile.format, _save)

Image.register_extensions(PpmImageFile.format, [".pbm", ".pgm", ".ppm", ".pnm"])

Image.register_mime(PpmImageFile.format, "image/x-portable-anymap")
