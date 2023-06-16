# Copyright (C) 2023 Mandiant, Inc. All Rights Reserved.

import re
import sys
import struct
import logging
import argparse
from typing import List, Iterable, Optional

import pefile

from floss.results import StaticString, StringEncoding

logger = logging.getLogger(__name__)

MIN_STR_LEN = 4


def extract_go_strings(
    sample: str,
    min_length,
) -> Iterable[StaticString]:
    """
    Get Go strings from a PE file.
    Reference: https://github.com/mandiant/flare-floss/issues/779
    """

    try:
        pe = pefile.PE(sample)
    except pefile.PEFormatError as err:
        logger.debug(f"invalid PE file: {err}")
        raise ValueError("Invalid PE header")

    if pe.FILE_HEADER.Machine == pefile.MACHINE_TYPE["IMAGE_FILE_MACHINE_AMD64"]:
        """
        .data:0000000000770F20 3D 68 60 00 00 00+off_770F20      dq offset aString
        .data:0000000000770F28 15                                db  15h
        .data:0000000000770F29 00                                db    0
        """
        alignment = 0x10  # 16
        fmt = "<QQ"

        # See https://github.com/mandiant/flare-floss/issues/805#issuecomment-1590472813 for regex explanation
        combinedregex = re.compile(
            b"\x48\xba(........)|\x48\xb8(........)|\x81\x78\x08(....)|\x81\x79\x08(....)|\x66\x81\x78\x0c(..)|\x66\x81\x79\x0c(..)|\x80\x78\x0e(.)|\x80\x79\x0e(.)"
        )


        # For go version >= 1.17
        #
        # .text:48FF81 48 83 FB 13                                   cmp     rbx, 13h
        # .text:48FF85 75 15                                         jnz     short loc_48FF9C
        # .text:48FF87 48 8D 1D 51 DC 01 00                          lea     rbx, unk_4ADBDF
        # .text:48FF8E B9 13 00 00 00                                mov     ecx, 13h

        longstring = re.compile(b"\x48\x8D\x1D(....)\xB9(....)")
        longstring2 = re.compile(b"\x48\x83\xFB(.)(.){2,5}\x48\x8D\x1D(....)")

    elif pe.FILE_HEADER.Machine == pefile.MACHINE_TYPE["IMAGE_FILE_MACHINE_I386"]:
        """
        .data:102A78D0 E3 9A 17 10                       dd offset aString
        .data:102A78D4 12                                db  12h
        """
        alignment = 0x8
        fmt = "<II"

        # See https://github.com/mandiant/flare-floss/issues/805#issuecomment-1590510957 for regex explanation
        combinedregex = re.compile(
            b"\x81\xf9(....)|\x81\x38(....)|\x81\x7d\x00(....)|\x81\x3B(....)|\x66\x81\xf9(..)|\x66\x81\x7b\x04(..)|\x66\x81\x78\x04(..)|\x66\x81\x7d\x04(..)|\x80\x7b\x06(.)|\x80\x7d\x06(.)|\x80\xf8(.)|\x80\x78\x06(.)"
        )
    else:
        raise ValueError("unhandled architecture")

    for section in pe.sections:
        try:
            section_name = section.Name.partition(b"\x00")[0].decode("utf-8")
        except UnicodeDecodeError:
            continue

        if section_name == ".text":
            section_va = section.VirtualAddress
            section_size = section.SizeOfRawData
            section_data = section.get_data(section_va, section_size)
            addr = 0

            for m in longstring.finditer(section_data):
                format = "<I"
                s_off = struct.unpack(format, m.group(1))[0]
                s_size = struct.unpack(format, m.group(2))[0]

                s_rva = s_off + m.end() + section_va
                try:
                    string = pe.get_string_at_rva(s_rva, s_size).decode("ascii")
                    if string.isprintable() and string != "" and len(string) >= min_length:
                        yield StaticString(string=string, offset=addr, encoding=StringEncoding.ASCII)
                except UnicodeDecodeError:
                    continue

            for m in longstring2.finditer(section_data):
                s_off = struct.unpack("<I", m.group(3))[0]
                s_size = struct.unpack("<B", m.group(1))[0]

                s_rva = s_off + m.end() + section_va
                try:
                    string = pe.get_string_at_rva(s_rva, s_size).decode("ascii")
                    if string.isprintable() and string != "" and len(string) >= min_length:
                        yield StaticString(string=string, offset=addr, encoding=StringEncoding.ASCII)
                except UnicodeDecodeError:
                    continue

        if section_name == ".text":
            section_va = section.VirtualAddress
            section_size = section.SizeOfRawData
            section_data = section.get_data(section_va, section_size)

            strings = re.findall(combinedregex, section_data)

            for string_tuple in strings:
                for string in string_tuple:
                    if string != b"":
                        try:
                            decoded_string = string.decode("utf-8")
                            if decoded_string.isprintable() and len(string) >= min_length:
                                addr = 0
                                yield StaticString(string=string, offset=addr, encoding=StringEncoding.ASCII)
                        except UnicodeDecodeError:
                            pass

        if section_name in (".rdata", ".data"):
            section_va = section.VirtualAddress
            section_size = section.SizeOfRawData
            section_data = section.get_data(section_va, section_size)

            for i in range(0, len(section_data) - alignment // 2, alignment // 2):
                try:
                    curr = section_data[i : i + alignment]
                    s_off, s_size = struct.unpack(fmt, curr)

                    if not s_off and not (1 <= s_size < 128):
                        continue

                    s_rva = s_off - pe.OPTIONAL_HEADER.ImageBase

                    if not pe.get_section_by_rva(s_rva):
                        continue

                    addr = pe.OPTIONAL_HEADER.ImageBase + section_va + i

                    try:
                        string = pe.get_string_at_rva(s_rva, s_size).decode("ascii")
                    except UnicodeDecodeError:
                        continue

                    if (
                        len(string) >= min_length and len(string) == s_size
                    ):  # if the string is greater than the minimum length
                        yield StaticString(string=string, offset=addr, encoding=StringEncoding.ASCII)
                except Exception as e:
                    logger.error(f"Error: {e}")
                    raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Get Go strings")
    parser.add_argument("path", help="file or path to analyze")
    parser.add_argument(
        "-n",
        "--minimum-length",
        dest="min_length",
        type=int,
        default=MIN_STR_LEN,
        help="minimum string length",
    )
    args = parser.parse_args(args=argv)

    static_strings = extract_go_strings(args.path, min_length=args.min_length)

    for strings_obj in static_strings:
        string = strings_obj.string
        print(string)


if __name__ == "__main__":
    sys.exit(main())
