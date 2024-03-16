# Copyright (C) 2023 Mandiant, Inc. All Rights Reserved.
import sys
import logging
import pathlib
import argparse
import itertools
from typing import List, Tuple, Iterable, Optional

import pefile
import binary2strings as b2s

from floss.results import StaticString, StringEncoding
from floss.language.utils import (
    find_lea_xrefs,
    find_mov_xrefs,
    find_push_xrefs,
    get_rdata_section,
    get_struct_string_candidates,
)

logger = logging.getLogger(__name__)

MIN_STR_LEN = 4


def fix_b2s_wide_strings(
    strings: List[Tuple[str, str, Tuple[int, int], bool]],
    min_length: int,
    buffer: bytes,
) -> List[Tuple[str, str, Tuple[int, int], bool]]:
    """
    Handles potential misidentification of UTF-16 strings during extraction.

    This function attempts to correct cases where wide strings (likely UTF-16 encoded) have been incorrectly parsed as UTF-8 strings. It does this by re-encoding and re-extracting the string.

    Args:
        strings: A list of tuples containing extracted strings, their types, offsets, and other metadata.
        min_length: The minimum length for a string to be considered valid.
        buffer: The raw byte buffer being analyzed.

    Returns:
        List[Tuple[str, str, Tuple[int, int], bool]]: A modified list of string tuples, potentially with corrected strings.
    """
    # TODO(mr-tz): b2s may parse wide strings where there really should be utf-8 strings
    #  handle special cases here until fixed
    #  https://github.com/mandiant/flare-floss/issues/867
    fixed_strings: List[Tuple[str, str, Tuple[int, int], bool]] = list()
    last_fixup: Optional[Tuple[str, str, Tuple[int, int], bool]] = None
    for string in strings:
        s = string[0]
        string_type = string[1]
        start = string[2][0]

        if string_type == "WIDE_STRING":
            sd = s.encode("utf-16le", "ignore")
            # utf-8 strings will not start with \x00
            if sd[0] == 0:
                new_string = b2s.extract_string(buffer[start + 1 :])
                last_fixup = (
                    new_string[0],
                    new_string[1],
                    (new_string[2][0] + start + 1, new_string[2][1] + start + 1),
                    new_string[3],
                )
                if len(last_fixup[0]) < min_length:
                    last_fixup = None
        else:
            if last_fixup and s in last_fixup[0]:
                fixed_strings.append(last_fixup)
            else:
                fixed_strings.append(string)
            last_fixup = None
    return fixed_strings


def filter_and_transform_utf8_strings(
    strings: List[Tuple[str, str, Tuple[int, int], bool]],
    start_rdata: int,
) -> List[StaticString]:
    """
    Filters extracted strings, transforms UTF-8 strings, and creates StaticString objects.

    This function focuses on UTF-8 encoded strings. It removes newline characters, calculates the correct offsets within the file, and constructs StaticString objects.

    Args:
        strings: A list of tuples containing extracted strings, their types, offsets, and other metadata.
        start_rdata: The starting offset of the .rdata section within the file.

    Returns:
        List[StaticString]: A list of StaticString objects representing the filtered and transformed UTF-8 strings.
    """
    transformed_strings = []

    for string in strings:
        s = string[0]
        string_type = string[1]
        start = string[2][0] + start_rdata

        if string_type != "UTF8":
            continue

        # our static algorithm does not extract new lines either
        s = s.replace("\n", "")
        transformed_strings.append(
            StaticString(string=s, offset=start, encoding=StringEncoding.UTF8)
        )

    return transformed_strings


def split_strings(
    static_strings: List[StaticString], address: int, min_length: int
) -> None:
    """
    Splits StaticString objects if an address falls within their string data.

    This function operates directly on the provided `static_strings` list.  It checks if a given address lies within an existing StaticString. If so, it splits the string into two, preserving both parts if they meet the minimum length requirement.

    Args:
        static_strings: A list of StaticString objects.
        address: The address to check against the string boundaries.
        min_length: The minimum length for a string to be considered valid.
    """

    for string in static_strings:
        if string.offset < address < string.offset + len(string.string):
            rust_string = string.string[0 : address - string.offset]
            rest = string.string[address - string.offset :]

            if len(rust_string) >= min_length:
                static_strings.append(
                    StaticString(
                        string=rust_string,
                        offset=string.offset,
                        encoding=StringEncoding.UTF8,
                    )
                )
            if len(rest) >= min_length:
                static_strings.append(
                    StaticString(
                        string=rest, offset=address, encoding=StringEncoding.UTF8
                    )
                )

            # remove string from static_strings
            for static_string in static_strings:
                if static_string == string:
                    static_strings.remove(static_string)
                    return

            return


def extract_rust_strings(sample: pathlib.Path, min_length: int) -> List[StaticString]:
    """
    Extracts potential Rust strings from a file.

    This function likely employs heuristics and techniques tailored to identifying strings that are typically present in Rust-compiled binaries. It leverages the `get_string_blob_strings` function, implying a focus on the string blob region.

    Args:
        sample: The path to the file to analyze.
        min_length: The minimum length for a string to be considered valid.

    Returns:
        List[StaticString]: A list of extracted StaticString objects.
    """

    p = pathlib.Path(sample)
    buf = p.read_bytes()
    pe = pefile.PE(data=buf, fast_load=True)

    rust_strings: List[StaticString] = list()
    rust_strings.extend(get_string_blob_strings(pe, min_length))

    return rust_strings


def get_static_strings_from_rdata(sample, static_strings) -> List[StaticString]:
    """
    Filters StaticString objects based on the .rdata section of a PE file.

    This function assumes the existence of a pre-populated list of StaticString objects. It filters these strings, keeping only those whose offsets fall within the boundaries of the .rdata section of a PE file.

    Args:
        sample: The path to the PE file.
        static_strings: A list of StaticString objects.

    Returns:
        List[StaticString]:  A filtered list of StaticString objects that are located within the .rdata section.
    """
    pe = pefile.PE(data=pathlib.Path(sample).read_bytes(), fast_load=True)

    try:
        rdata_section = get_rdata_section(pe)
    except ValueError:
        return []

    start_rdata = rdata_section.PointerToRawData
    end_rdata = start_rdata + rdata_section.SizeOfRawData

    return list(filter(lambda s: start_rdata <= s.offset < end_rdata, static_strings))


def get_string_blob_strings(pe: pefile.PE, min_length: int) -> Iterable[StaticString]:
    """
     Extracts strings from the .rdata section of a PE file, focusing on UTF-8 strings with a minimum length.

    This function handles architecture-specific xrefs to find strings efficiently without reading all candidate strings, which may be numerous. It's tailored for Rust binaries but applicable to other PE files.

    Args:
        pe (pefile.PE): The PE file from which to extract strings.
        min_length (int): The minimum length of strings to extract.

    Returns:
        Iterable[StaticString]: An iterable of `StaticString` objects found within the .rdata section of the given PE file.

    Note:
        The function prioritizes performance and accuracy by leveraging specific characteristics of Rust binaries and PE file structure.
    """
    image_base = pe.OPTIONAL_HEADER.ImageBase

    try:
        rdata_section = get_rdata_section(pe)
    except ValueError as e:
        logger.error("cannot extract rust strings: %s", e)
        return []

    start_rdata = rdata_section.PointerToRawData
    end_rdata = start_rdata + rdata_section.SizeOfRawData
    virtual_address = rdata_section.VirtualAddress
    pointer_to_raw_data = rdata_section.PointerToRawData
    buffer_rdata = rdata_section.get_data()

    # extract utf-8 and wide strings, latter not needed here
    strings = b2s.extract_all_strings(buffer_rdata, min_length)
    fixed_strings = fix_b2s_wide_strings(strings, min_length, buffer_rdata)

    # select only UTF-8 strings and adjust offset
    static_strings = filter_and_transform_utf8_strings(fixed_strings, start_rdata)

    # TODO(mr-tz) - handle miss in rust-hello64.exe
    #  .rdata:00000001400C1270 0A                      aPanickedAfterP db 0Ah                  ; DATA XREF: .rdata:00000001400C12B8↓o
    #  .rdata:00000001400C1271 70 61 6E 69 63 6B 65 64…                db 'panicked after panic::always_abort(), aborting.',0Ah,0
    #  .rdata:00000001400C12A2 00 00 00 00 00 00                       align 8

    struct_string_addrs = map(lambda c: c.address, get_struct_string_candidates(pe))

    if pe.FILE_HEADER.Machine == pefile.MACHINE_TYPE["IMAGE_FILE_MACHINE_I386"]:
        xrefs_lea = find_lea_xrefs(pe)
        xrefs_push = find_push_xrefs(pe)
        xrefs_mov = find_mov_xrefs(pe)
        xrefs = itertools.chain(struct_string_addrs, xrefs_lea, xrefs_push, xrefs_mov)

    elif pe.FILE_HEADER.Machine == pefile.MACHINE_TYPE["IMAGE_FILE_MACHINE_AMD64"]:
        xrefs_lea = find_lea_xrefs(pe)
        xrefs = itertools.chain(struct_string_addrs, xrefs_lea)

        # TODO(mr-tz) - handle movdqa rust-hello64.exe
        #  .text:0000000140026046 66 0F 6F 05 02 71 09 00                 movdqa  xmm0, cs:xmmword_1400BD150
        #  .text:000000014002604E 66 0F 6F 0D 0A 71 09 00                 movdqa  xmm1, cs:xmmword_1400BD160
        #  .text:0000000140026056 66 0F 6F 15 12 71 09 00                 movdqa  xmm2, cs:xmmword_1400BD170

    else:
        logger.error("unsupported architecture: %s", pe.FILE_HEADER.Machine)
        return []

    for addr in xrefs:
        address = addr - image_base - virtual_address + pointer_to_raw_data

        if not (start_rdata <= address < end_rdata):
            continue

        split_strings(static_strings, address, min_length)

    return static_strings


def main(argv=None):
    """
    Parses command-line arguments, coordinates Rust string extraction, and displays results.

    Sets up logging, parses arguments, extracts strings using the `extract_rust_strings` function, sorts the results, and prints them to the console.

    Args:
        argv:  Command-line arguments (Default: None)
    """
    parser = argparse.ArgumentParser(description="Get Rust strings")
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

    logging.basicConfig(level=logging.DEBUG)

    rust_strings = sorted(
        extract_rust_strings(args.path, args.min_length), key=lambda s: s.offset
    )
    for string in rust_strings:
        print(f"{string.offset:#x}: {string.string}")


if __name__ == "__main__":
    sys.exit(main())
