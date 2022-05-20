# Copyright (C) 2017 Mandiant, Inc. All Rights Reserved.
import re
import time
import inspect
import logging
import argparse
import contextlib
from typing import Set, Iterable
from collections import OrderedDict

import tqdm
import tabulate
import viv_utils
import envi.archs
import viv_utils.emulator_drivers
from envi import Emulator

import floss.strings
import floss.logging_

from .const import MEGABYTE, MAX_STRING_LENGTH
from .results import StaticString
from .api_hooks import ENABLED_VIV_DEFAULT_HOOKS

STACK_MEM_NAME = "[stack]"

logger = floss.logging_.getLogger(__name__)


class ExtendAction(argparse.Action):
    # stores a list, and extends each argument value to the list
    # Since Python 3.8 argparse supports this
    # TODO: remove this code when only supporting Python 3.8+
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None) or []
        items.extend(values)
        setattr(namespace, self.dest, items)


def set_vivisect_log_level(level) -> None:
    logging.getLogger("vivisect").setLevel(level)
    logging.getLogger("vivisect.base").setLevel(level)
    logging.getLogger("vivisect.impemu").setLevel(level)
    logging.getLogger("vtrace").setLevel(level)
    logging.getLogger("envi").setLevel(level)
    logging.getLogger("envi.codeflow").setLevel(level)


def make_emulator(vw) -> Emulator:
    """
    create an emulator using consistent settings.
    """
    emu = vw.getEmulator(logwrite=True, taintbyte=b"\xFE")
    remove_stack_memory(emu)
    emu.initStackMemory(stacksize=int(0.5 * MEGABYTE))
    emu.setStackCounter(emu.getStackCounter() - int(0.25 * MEGABYTE))
    # do not short circuit rep prefix
    emu.setEmuOpt("i386:repmax", 256)  # 0 == no limit on rep prefix
    viv_utils.emulator_drivers.remove_default_viv_hooks(emu, allow_list=ENABLED_VIV_DEFAULT_HOOKS)
    return emu


def remove_stack_memory(emu: Emulator):
    # TODO this is a hack while vivisect's initStackMemory() has a bug
    memory_snap = emu.getMemorySnap()
    for i in range((len(memory_snap) - 1), -1, -1):
        (_, _, info, _) = memory_snap[i]
        if info[3] == STACK_MEM_NAME:
            del memory_snap[i]
            emu.setMemorySnap(memory_snap)
            emu.stack_map_base = None
            return
    raise ValueError("`STACK_MEM_NAME` not in memory map")


def dump_stack(emu):
    """
    Convenience debugging routine for showing
     state current state of the stack.
    """
    esp = emu.getStackCounter()
    stack_str = ""
    for i in range(16, -16, -4):
        if i == 0:
            sp = "<= SP"
        else:
            sp = "%02x" % (-i)
        stack_str = "%s\n0x%08x - 0x%08x %s" % (stack_str, (esp - i), floss.utils.get_stack_value(emu, -i), sp)
    logger.trace(stack_str)
    return stack_str


def get_stack_value(emu, offset):
    return emu.readMemoryFormat(emu.getStackCounter() + offset, "<P")[0]


def getPointerSize(vw):
    if isinstance(vw.arch, envi.archs.amd64.Amd64Module):
        return 8
    elif isinstance(vw.arch, envi.archs.i386.i386Module):
        return 4
    else:
        raise NotImplementedError("unexpected architecture: %s" % (vw.arch.__class__.__name__))


def get_imagebase(vw):
    basename = vw.getFileByVa(vw.getEntryPoints()[0])
    return vw.getFileMeta(basename, "imagebase")


def get_vivisect_meta_info(vw, selected_functions, decoding_function_features):
    info = OrderedDict()
    entry_points = vw.getEntryPoints()
    basename = None
    if entry_points:
        basename = vw.getFileByVa(entry_points[0])

    # "blob" is the filename for shellcode
    if basename and basename != "blob":
        version = vw.getFileMeta(basename, "Version")
        md5sum = vw.getFileMeta(basename, "md5sum")
        baseva = hex(vw.getFileMeta(basename, "imagebase"))
    else:
        version = "N/A"
        md5sum = "N/A"
        baseva = "N/A"

    info["version"] = version
    info["MD5 Sum"] = md5sum
    info["format"] = vw.getMeta("Format")
    info["architecture"] = vw.getMeta("Architecture")
    info["platform"] = vw.getMeta("Platform")
    disc = vw.getDiscoveredInfo()[0]
    undisc = vw.getDiscoveredInfo()[1]
    info["percentage of discovered executable surface area"] = "%.1f%% (%s / %s)" % (
        disc * 100.0 / (disc + undisc),
        disc,
        disc + undisc,
    )
    info["base VA"] = baseva
    info["entry point(s)"] = ", ".join(map(hex, entry_points))
    info["number of imports"] = len(vw.getImports())
    info["number of exports"] = len(vw.getExports())
    info["number of functions"] = len(vw.getFunctions())

    if selected_functions:
        meta = []
        for fva in selected_functions:
            if is_thunk_function(vw, fva) or viv_utils.flirt.is_library_function(vw, fva):
                continue

            xrefs_to = len(vw.getXrefsTo(fva))
            num_args = len(vw.getFunctionArgs(fva))
            function_meta = vw.getFunctionMetaDict(fva)
            instr_count = function_meta.get("InstructionCount")
            block_count = function_meta.get("BlockCount")
            size = function_meta.get("Size")
            score = round(decoding_function_features.get(fva, {}).get("score", 0), 3)
            meta.append((hex(fva), score, xrefs_to, num_args, size, block_count, instr_count))
        info["selected functions' info"] = "\n%s" % tabulate.tabulate(
            meta, headers=["fva", "score", "#xrefs", "#args", "size", "#blocks", "#instructions"]
        )

    return info


def hex(i):
    return "0x%x" % (i)


# TODO ideally avoid emulation in the first place
#  libary detection appears to fail, called via __amsg_exit or __abort
#  also see issue #296 for another possible solution
FP_STRINGS = (
    "R6016",
    "R6030",
    "Program: ",
    "Runtime Error!",
    "bad locale name",
    "ios_base::badbit set",
    "ios_base::eofbit set",
    "ios_base::failbit set",
    "- CRT not initialized",
    "program name unknown>",
    "<program name unknown>",
    "- floating point not loaded",
    "Program: <program name unknown>",
    "- not enough space for thread data",
    # all printable ASCII chars
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
)


def extract_strings(buffer: bytes, min_length: int, exclude: Set[str] = None) -> Iterable[StaticString]:
    if len(buffer) < min_length:
        return

    for s in floss.strings.extract_ascii_unicode_strings(buffer):
        if len(s.string) > MAX_STRING_LENGTH:
            continue

        if s.string in FP_STRINGS:
            continue

        decoded_string = strip_string(s.string)

        if len(decoded_string) < min_length:
            logger.trace("filtered: %s -> %s", s.string, decoded_string)
            continue

        logger.trace("strip: %s -> %s", s.string, decoded_string)

        if exclude and decoded_string in exclude:
            continue

        yield StaticString(decoded_string, s.offset, s.encoding)


# FP string starts
# pVA, VA, 0VA, ..VA
FP_FILTER_PREFIX_1 = re.compile(r"^.{0,2}[0pP]?[]^\[_\\V]A")
# FP string ends
FP_FILTER_SUFFIX_1 = re.compile(r"[0pP]?[VWU][A@]$|Tp$")
# same printable ASCII char 4 or more consecutive times
FP_FILTER_REP_CHARS_1 = re.compile(r"([ -~])\1{3,}")
# same 4 printable ASCII chars 5 or more consecutive times
# /v7+/v7+/v7+/v7+
# ignore space and % for potential format strings, like %04d%02d%02d%02d%02d
FP_FILTER_REP_CHARS_2 = re.compile(r"([^% ]{4})\1{4,}")

# be stricter removing FP strings for shorter strings
MAX_STRING_LENGTH_FILTER_STRICT = 6
# e.g. [ESC], [Alt], %d.dll
FP_FILTER_STRICT_INCLUDE = re.compile(r"^\[.*?]$|%[sd]")
# remove special characters
FP_FILTER_STRICT_SPECIAL_CHARS = re.compile(r"[^A-Za-z0-9.]")
# TODO eTpH., gTpd, BTpp, etc.
# TODO DEEE, RQQQ
FP_FILTER_STRICT_KNOWN_FP = re.compile(r"^O.*A$")


def strip_string(s) -> str:
    """
    Return string stripped from false positive (FP) pre- or suffixes.
    :param s: input string
    :return: string stripped from FP pre- or suffixes
    """
    for reg in (FP_FILTER_PREFIX_1, FP_FILTER_SUFFIX_1, FP_FILTER_REP_CHARS_1, FP_FILTER_REP_CHARS_2):
        s = re.sub(reg, "", s)
    if len(s) <= MAX_STRING_LENGTH_FILTER_STRICT:
        if not re.match(FP_FILTER_STRICT_INCLUDE, s):
            for reg2 in (FP_FILTER_STRICT_KNOWN_FP, FP_FILTER_STRICT_SPECIAL_CHARS):
                s = re.sub(reg2, "", s)
    return s


@contextlib.contextmanager
def redirecting_print_to_tqdm():
    """
    tqdm (progress bar) expects to have fairly tight control over console output.
    so calls to `print()` will break the progress bar and make things look bad.
    so, this context manager temporarily replaces the `print` implementation
    with one that is compatible with tqdm.
    via: https://stackoverflow.com/a/42424890/87207
    """
    old_print = print

    def new_print(*args, **kwargs):

        # If tqdm.tqdm.write raises error, use builtin print
        try:
            tqdm.tqdm.write(*args, **kwargs)
        except:
            old_print(*args, **kwargs)

    try:
        # Globaly replace print with new_print
        inspect.builtins.print = new_print
        yield
    finally:
        inspect.builtins.print = old_print


@contextlib.contextmanager
def timing(msg):
    t0 = time.time()
    yield
    t1 = time.time()
    logger.trace("perf: %s: %0.2fs", msg, t1 - t0)


def get_runtime_diff(time0):
    return round(time.time() - time0, 4)


def is_all_zeros(buffer: bytes):
    return all([b == 0 for b in buffer])


def get_progress_bar(functions, disable_progress, desc="", unit=""):
    pbar = tqdm.tqdm
    if disable_progress:
        # do not use tqdm to avoid unnecessary side effects when caller intends
        # to disable progress completely
        pbar = lambda s, *args, **kwargs: s
    return pbar(functions, desc=desc, unit=unit)


def is_thunk_function(vw, function_address):
    return vw.getFunctionMetaDict(function_address).get("Thunk", False)
