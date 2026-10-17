#!/usr/bin/env python3
"""Build the original GT3 Paint Shop menu from stock MBL/PMB files.

This is the version with the Paint Shop button in GT Auto, a rotating owned car,
the dealership colour selector, and BACK. Colours preview as you scroll.
It doesn't permanently repaint the car yet.

Only creates three menu files. No ELF edits, database edits, or PNACH needed.
Python 3.10+; no extra packages.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

VERSION = '1.0.0'
PAGE = 'at/ps/ps'
ORIGINAL_SHA256 = {
    'gt_mode.mbl': '4c8b47541800045c58df740291a9ea3f56993c9b791db328706787970128004d',
    'gt_mode601.pmb': '34ab3c881a114b434864571b9a3a0f553ad91d0af042afe7f9d30ab7f4a634b5',
    'gt_mode610.pmb': 'a7f73e3d998f4b15d259c08047d8f2f87477a2f4defc1752d3b203fb204b15b8',
    'gt_mode310.pmb': '34cb226cf4997cd4cce97ced747fa878e89aa847b7a6691d453691385231b85e',
}
# Hashes from the working PAL/BR files. I use these to catch a bad rebuild.
TESTED_M2_SHA256 = {
    'gt_mode.mbl': 'e48318f633bae7aac253127e089363730cbf414e304a267c4240b3525c75d6cb',
    'gt_mode601.pmb': '07450074314fc77328055f96686c9fbd345dc8960d003e3820d3447857fb8afe',
    'gt_mode610.pmb': 'd03400ced46bb12aff033fe484c9ba395c1ec32e86e80d849f04ea48166c3287',
}
INPUT_NAMES = tuple(ORIGINAL_SHA256)
OUTPUT_NAMES = ('gt_mode.mbl', 'gt_mode601.pmb', 'gt_mode610.pmb')
MAX_BINARY = 128 * 1024 * 1024
MAX_ENTRIES = 20000
MAX_IMAGE_UNCOMPRESSED = 128 * 1024 * 1024


class BuildError(Exception):
    """Something wasn't what we expected, so don't write any files."""


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class BinaryReader:
    def __init__(self, data: bytes):
        self.b = data

    def bounds(self, pos: int, size: int = 1):
        if pos < 0 or size < 0 or pos + size > len(self.b):
            raise BuildError(f'Invalid binary pointer {pos:#x}+{size:#x} in {len(self.b):#x} bytes')

    def u32(self, pos: int) -> int:
        self.bounds(pos, 4)
        return struct.unpack_from('<I', self.b, pos)[0]

    def u16(self, pos: int) -> int:
        self.bounds(pos, 2)
        return struct.unpack_from('<H', self.b, pos)[0]

    def string(self, pos: int, maximum: int | None = None) -> str:
        self.bounds(pos)
        end = self.b.find(b'\x00', pos, min(pos + maximum, len(self.b)) if maximum else len(self.b))
        if end < 0:
            raise BuildError(f'Unterminated string at {pos:#x}')
        return self.b[pos:end].decode('euc_jp', 'surrogateescape')

    def pointers(self, count: int, pos: int) -> list[int]:
        if count > MAX_ENTRIES:
            raise BuildError(f'Unexpected pointer count: {count}')
        self.bounds(pos, 4 * count)
        return [self.u32(pos + 4 * i) for i in range(count)]


class BinaryWriter:
    def __init__(self):
        self.b = bytearray()

    def alloc(self, count: int) -> int:
        p = len(self.b)
        self.b.extend(bytes(count))
        return p

    def put(self, pos: int, layout: str, *values):
        struct.pack_into('<' + layout, self.b, pos, *values)

    def fixed_string(self, pos: int, value: str, limit: int = 64):
        encoded = value.encode('euc_jp', 'surrogateescape')
        if b'\0' in encoded or len(encoded) >= limit:
            raise BuildError(f'Text does not fit in {limit} bytes: {value!r}')
        self.b[pos:pos + limit] = encoded + bytes(limit - len(encoded))

    def variable_string(self, value: str) -> int:
        encoded = value.encode('euc_jp', 'surrogateescape')
        n = (len(encoded) // 4 + 1) * 4
        p = self.alloc(n)
        self.fixed_string(p, value, n)
        return p

    def pointer_array(self, entries: list, serializer) -> int:
        pos = self.alloc(4 * len(entries))
        for i, entry in enumerate(entries):
            self.put(pos + 4 * i, 'I', serializer(entry))
        return pos

    def pad(self, align: int):
        # These PMBs pad texture blocks with 0x50 ('P'), not zeroes.
        self.b.extend(b'P' * ((-len(self.b)) % align))


def parse_mbl(raw: bytes) -> list:
    r = BinaryReader(raw)

    def child(pos: int, depth: int = 0):
        if depth > 64:
            raise BuildError('Unexpected deeply nested MBL')
        r.bounds(pos, 332)
        header = ['SUB'] + [r.string(pos + 64 * j, 64) for j in range(5)]
        header += [r.u16(pos + 320), r.u16(pos + 322)]
        children = [child(p, depth + 1) for p in r.pointers(r.u32(pos + 324), r.u32(pos + 328))]
        return [header, children]

    pages = []
    for pos in r.pointers(r.u32(0), r.u32(4)):
        r.bounds(pos, 268)
        header = ['MAIN', r.string(pos, 64), r.u16(pos + 64), r.u16(pos + 66)]
        header += [r.string(pos + x, 64) for x in (68, 132, 196)]
        pages.append([header, [child(p) for p in r.pointers(r.u32(pos + 260), r.u32(pos + 264))]])
    return ['ROOT', 'mbl40', pages]


def serialize_mbl(root: list) -> bytes:
    if root[:2] != ['ROOT', 'mbl40']:
        raise BuildError('Unsupported MBL tree')
    w = BinaryWriter()
    w.alloc(8)

    def child(entry):
        header, children = entry
        pos = w.alloc(332)
        for i, text in enumerate(header[1:6]):
            w.fixed_string(pos + 64 * i, text)
        w.put(pos + 320, 'HHI', header[6], header[7], len(children))
        w.put(pos + 328, 'I', w.pointer_array(children, child))
        return pos

    def page(entry):
        header, children = entry
        pos = w.alloc(268)
        w.fixed_string(pos, header[1])
        w.put(pos + 64, 'HH', header[2], header[3])
        for i, text in enumerate(header[4:7]):
            w.fixed_string(pos + 68 + 64 * i, text)
        w.put(pos + 260, 'I', len(children))
        w.put(pos + 264, 'I', w.pointer_array(children, child))
        return pos

    w.put(0, 'II', len(root[2]), w.pointer_array(root[2], page))
    return bytes(w.b)


def parse_pmb(raw: bytes) -> tuple[list, list[tuple[int, bytes]]]:
    r = BinaryReader(raw)

    def element(pos, depth=0):
        if depth > 64:
            raise BuildError('Unexpected deeply nested PMB')
        r.bounds(pos, 108)
        header = ['SUB', r.string(pos, 64)] + [r.u32(pos + 64 + 4 * j) for j in range(6)]
        flags = header[2]
        result = [header, [], [], [], [], []]
        if (flags & 0x30000) == 0x10000:
            result[1] = ['FUNC', r.string(r.u32(r.u32(pos + 88)))]
        if flags & 8:
            result[2] = [element(r.u32(pos + 92), depth + 1)]
        if flags & 1:
            result[3] = ['IMAGE'] + [r.u32(r.u32(pos + 96) + 4 * j) for j in range(9)]
        if flags & 2:
            result[4] = ['BOX'] + [r.u32(r.u32(pos + 100) + 4 * j) for j in range(4)]
        if flags & 4:
            t = r.u32(pos + 104)
            result[5] = ['TEXT1', r.u16(t), r.u16(t + 2), r.u32(t + 4),
                         r.string(r.u32(t + 8)), r.string(r.u32(t + 12))]
        return result

    def group(pos, kind):
        r.bounds(pos, 80)
        children = [element(p) for p in r.pointers(r.u32(pos + 64), r.u32(pos + 68))]
        inner = [element(p) if kind == 'LAYER' else group(p, 'LAYER')
                 for p in r.pointers(r.u32(pos + 72), r.u32(pos + 76))]
        return [[kind, r.string(pos, 64)], children, inner]

    tree = ['ROOT', 'pmb40', [group(p, 'MAIN') for p in r.pointers(r.u32(0), r.u32(4))]]
    image_count, table = r.u32(8), r.u32(12)
    if image_count > MAX_ENTRIES:
        raise BuildError(f'Unreasonably large image count: {image_count}')
    r.bounds(table, image_count * 8)
    images = []
    for index in range(image_count):
        size, off = r.u32(table + 8 * index), r.u32(table + 8 * index + 4)
        end = r.u32(table + 8 * (index + 1) + 4) if index + 1 < image_count else len(raw)
        r.bounds(off, end - off)
        if size > MAX_IMAGE_UNCOMPRESSED:
            raise BuildError('Unreasonably large decompressed image')
        chunk = raw[off:end]
        if chunk.startswith(b'\x1f\x8b'):
            try:
                decompressor = zlib.decompressobj(31)
                decompressed = decompressor.decompress(chunk, size + 1)
                if len(decompressed) != size or not decompressor.eof:
                    raise BuildError(f'Bad compressed PMB image #{index}')
                payload = chunk[:len(chunk) - len(decompressor.unused_data)]
            except zlib.error as exc:
                raise BuildError(f'Cannot decompress PMB image #{index}: {exc}') from exc
        elif chunk.startswith(b'Tex1'):
            payload = chunk[:size]
        else:
            raise BuildError(f'Unsupported PMB image #{index}')
        images.append((size, payload))
    return tree, images


def serialize_pmb(tree: list, images: list[tuple[int, bytes]]) -> bytes:
    if tree[:2] != ['ROOT', 'pmb40']:
        raise BuildError('Unsupported PMB tree')
    w = BinaryWriter()
    w.alloc(16)

    def element(entry):
        header = entry[0]
        pos = w.alloc(108)
        w.fixed_string(pos, header[1])
        w.put(pos + 64, '6I', *header[2:])
        flags = header[2]
        if (flags & 0x30000) == 0x10000:
            func = w.alloc(4)
            w.put(pos + 88, 'I', func)
            w.put(func, 'I', w.variable_string(entry[1][1]))
        if flags & 8:
            if len(entry[2]) != 1:
                raise BuildError('PMB recursive SUB expected one child')
            w.put(pos + 92, 'I', element(entry[2][0]))
        if flags & 1:
            p = w.alloc(36)
            w.put(pos + 96, 'I', p)
            w.put(p, '9I', *entry[3][1:])
        if flags & 2:
            p = w.alloc(16)
            w.put(pos + 100, 'I', p)
            w.put(p, '4I', *entry[4][1:])
        if flags & 4:
            t = entry[5]
            p = w.alloc(16)
            w.put(pos + 104, 'I', p)
            w.put(p, 'HHI', *t[1:4])
            w.put(p + 8, 'II', w.variable_string(t[4]), w.variable_string(t[5]))
        return pos

    def group(entry):
        header, children, layers = entry
        pos = w.alloc(80)
        w.fixed_string(pos, header[1])
        w.put(pos + 64, 'II', len(children), w.pointer_array(children, element))
        w.put(pos + 72, 'II', len(layers), w.pointer_array(layers, element if header[0] == 'LAYER' else group))
        return pos

    w.put(0, 'II', len(tree[2]), w.pointer_array(tree[2], group))
    table = w.alloc(8 * len(images))
    w.put(8, 'II', len(images), table)
    for i, (size, payload) in enumerate(images):
        w.pad(16 if payload.startswith(b'Tex1') else 4)
        pos = w.alloc(len(payload))
        w.b[pos:] = payload
        w.put(table + 8 * i, 'II', size, pos)
    w.pad(4)
    return bytes(w.b)


def walk(value: Any):
    if isinstance(value, list):
        yield value
        for part in value:
            yield from walk(part)


def find_main(tree: list, name: str):
    matches = [page for page in tree[2] if page[0][1] == name]
    if len(matches) != 1:
        raise BuildError(f'Expected exactly one MAIN {name!r}; found {len(matches)}')
    return matches[0]


def find_element(items: list, name: str):
    matches = [item for item in items if item and isinstance(item[0], list) and item[0][1] == name]
    if len(matches) != 1:
        raise BuildError(f'Expected exactly one element {name!r}; found {len(matches)}')
    return matches[0]


def find_recursive(tree: list, name: str):
    matches = [entry for entry in walk(tree) if entry and isinstance(entry[0], list) and
               len(entry[0]) > 1 and entry[0][1] == name]
    if len(matches) != 1:
        raise BuildError(f'Expected exactly one recursive element {name!r}; found {len(matches)}')
    return matches[0]


def float_word(value: float | int) -> int:
    return struct.unpack('<I', struct.pack('<f', float(value)))[0]


def add_home_button(template, label: str, caption: str, x: int, y: int, width: int, height: int):
    item = copy.deepcopy(template)
    item[0][1] = label
    item[0][3:] = [float_word(v) for v in (x, y, 46, width, height)]
    for node in [item, item[2][0]]:
        node[0][2] = (node[0][2] & ~1) | 4
        node[3] = []
        node[5] = ['TEXT1', 1, 4, 0x7F000000, caption, 'sw_16#1']
    focus = item[2][0]
    focus[0][1] = label + '_focus'
    focus[0][3:] = [float_word(v) for v in (x, y, 80, width, height)]
    return item


def build(originals: dict[str, bytes]) -> tuple[dict[str, bytes], dict]:
    """Build everything in memory first. The source files are never edited."""
    mbl = parse_mbl(originals['gt_mode.mbl'])
    pmbl = {name: parse_pmb(originals[name]) for name in INPUT_NAMES if name.endswith('.pmb')}
    for name in INPUT_NAMES:
        if name.endswith('.mbl'):
            repacked = serialize_mbl(mbl)
        else:
            repacked = serialize_pmb(*pmbl[name])
        if repacked != originals[name]:
            raise BuildError(f'Input {name} does not round-trip byte-exactly. Unsupported variant or binary layout.')

    old_mbl = copy.deepcopy(mbl)
    original_pages = {name: copy.deepcopy(pmbl[name][0]) for name in pmbl}
    original_images = {name: list(pmbl[name][1]) for name in pmbl}
    p601, images601 = pmbl['gt_mode601.pmb']
    p610, images610 = pmbl['gt_mode610.pmb']
    p310, _ = pmbl['gt_mode310.pmb']

    if any(page[0][1] == PAGE for page in mbl[2]):
        raise BuildError('Paint Shop already exists in the MBL: provide the ORIGINAL files, not M2/M3 outputs.')
    if any(page[0][1] == PAGE for page in p610[2]):
        raise BuildError('Paint Shop already exists in PMB610: provide the ORIGINAL wheel PMB.')

    # The home menu uses gr/st/sl as internal names for wash/oil/wheels.
    # Keep that order. I tried this layout with pt added at the end.
    home = find_main(mbl, 'at/at')
    links = [(item[0][1], item[0][5], item[0][6]) for item in home[1]]
    if links != [('gr', 'at/cw/cw', 601), ('st', 'at/ot/ot', 601), ('sl', 'at/ws/ws', 610)]:
        raise BuildError(f'GT Auto home links differ from the layout I used: {links!r}')
    home[1].append([['SUB', 'pt', '', 'layer4', '', PAGE, 610, 0], []])
    dealership = find_main(mbl, 'cd/cd_jpn_daihatsu_car')
    hdr = copy.deepcopy(dealership[0])
    hdr[1], hdr[2], hdr[5], hdr[6] = PAGE, 610, 'at/at', ''
    mbl[2].append([hdr, [[['SUB', 'back', '', '', '', 'at/at', 601, 0], []]]])
    # GT3 looks these MAIN entries up in normal string order. No casefold here.
    mbl[2].sort(key=lambda x: x[0][1])

    # Add the fourth visible service button to the GT Auto home PMB.
    gt_auto = find_main(p601, 'at/at')
    sl = find_element(gt_auto[1], 'sl')
    gt_auto[1].append(add_home_button(sl, 'pt', 'PAINT SHOP', 122, 72, 320, 44))
    gt_auto[1].sort(key=lambda entry: entry[0][1])
    layer = copy.deepcopy(find_element(gt_auto[2], 'layer3'))
    layer[0][1] = 'layer4'
    gt_auto[2].append(layer)
    gt_auto[2].sort(key=lambda entry: entry[0][1])

    # Use the wheel-shop background, but the current owned-car renderer.
    # The dealership's colour widgets handle the swatches and name for us.
    wheel_page = find_main(p610, 'at/ws/ws_bridgestone')
    default_layer = find_element(wheel_page[2], 'defaultLayer')
    background = copy.deepcopy(find_element(default_layer[1], 'bg1'))
    title = copy.deepcopy(find_element(default_layer[1], 'maker_name'))
    title[0][1] = 'paint_title'
    title[0][4] = float_word(92)
    title[5][4] = 'PAINT SHOP'
    strip = copy.deepcopy(find_element(default_layer[1], 'price_bar'))
    strip[0][1] = 'paint_info_bar'

    owner_viewer = copy.deepcopy(find_element(default_layer[2], 'car_render_wheel_shop'))
    owner_viewer[0][1] = 'a_paint_owned_car'
    owner_viewer[1] = ['FUNC', 'car_render_mine']

    donor = find_main(p310, 'cd/cd_jpn_daihatsu_car')
    color_choices = copy.deepcopy(find_recursive(donor, 'car_color_box'))
    color_name = copy.deepcopy(find_recursive(donor, 'color_name_box'))
    color_name[0][4] = float_word(72)
    color_name[0][6] = float_word(440)
    color_name[5][3] = 0x7FFFFFFF

    back = add_home_button(find_element(donor[1], 'spec'), 'back', 'BACK', 518, 66, 90, 28)
    page = [['MAIN', PAGE], [back], [[['LAYER', 'defaultLayer'],
                                      [background, strip, title],
                                      [owner_viewer, color_choices, color_name]]]]
    page[2][0][1].sort(key=lambda item: item[0][1])
    page[2][0][2].sort(key=lambda item: item[0][1])
    p610[2].append(page)
    p610[2].sort(key=lambda entry: entry[0][1])

    # Don't accidentally pull in a dealer purchase or wheel fitting routine.
    functions = [node[1] for node in walk(page) if node and node[0] == 'FUNC']
    if sorted(functions) != sorted(['car_render_mine', 'car_color_box', 'color_name_box']):
        raise BuildError(f'Unexpected new page function registrations: {functions!r}')
    forbidden = {'buy_car', 'buy_wheel', 'fit_wheel', 'car_render_buy', 'set_car_code', 'set_focus_wheel_code'}
    values = {x for node in walk(page) for x in node if isinstance(x, str)}
    if values & forbidden:
        raise BuildError('New page unexpectedly includes a car-buying/wheel-fitting action')

    # No new textures. All image numbers must still point to a real entry.
    for pmb_name, (tree, images) in pmbl.items():
        for node in walk(tree):
            if node and node[0] == 'IMAGE' and not 0 <= node[1] < len(images):
                raise BuildError(f'Invalid image index {node[1]} in {pmb_name}')
    # Named MAIN tables need their original case-sensitive lexical ordering.
    for label, tree in [('MBL', mbl), ('PMB601', p601), ('PMB610', p610)]:
        names = [entry[0][1] for entry in tree[2]]
        if names != sorted(names) or len(set(names)) != len(names):
            raise BuildError(f'Unsorted/duplicate top-level pages in {label}')

    # Make sure the original menus are intact apart from our additions.
    old_mbl_by_name = {page[0][1]: page for page in old_mbl[2]}
    new_mbl_by_name = {page[0][1]: page for page in mbl[2]}
    for name, before in old_mbl_by_name.items():
        if name != 'at/at' and before != new_mbl_by_name[name]:
            raise BuildError(f'Unexpected existing MBL page modification: {name}')
    if old_mbl_by_name['at/at'][1] != new_mbl_by_name['at/at'][1][:-1]:
        raise BuildError('Existing GT Auto MBL links changed')
    for name in ('gt_mode601.pmb', 'gt_mode610.pmb', 'gt_mode310.pmb'):
        tree, images = pmbl[name]
        if images != original_images[name]:
            raise BuildError(f'Original texture bytes changed: {name}')
        new_page_index = {page[0][1]: page for page in tree[2]}
        for old_page in original_pages[name][2]:
            key = old_page[0][1]
            if name == 'gt_mode601.pmb' and key == 'at/at':
                continue
            if old_page != new_page_index[key]:
                raise BuildError(f'Existing PMB page altered: {name} {key}')
    if len(mbl[2]) != len(old_mbl[2]) + 1 or len(p610[2]) != len(original_pages['gt_mode610.pmb'][2]) + 1:
        raise BuildError('Unexpected MAIN page-count change')

    out = {
        'gt_mode.mbl': serialize_mbl(mbl),
        'gt_mode601.pmb': serialize_pmb(p601, images601),
        'gt_mode610.pmb': serialize_pmb(p610, images610),
    }
    # Read the output straight back. Catch bad pointers before someone boots it.
    if parse_mbl(out['gt_mode.mbl']) != mbl or serialize_mbl(parse_mbl(out['gt_mode.mbl'])) != out['gt_mode.mbl']:
        raise BuildError('Output MBL reparse/round-trip failed')
    for name, target_tree, images in [
        ('gt_mode601.pmb', p601, images601), ('gt_mode610.pmb', p610, images610),
    ]:
        decoded = parse_pmb(out[name])
        if decoded != (target_tree, images) or serialize_pmb(*decoded) != out[name]:
            raise BuildError(f'Output PMB reparse/round-trip failed: {name}')

    input_exact = all(digest(originals[name]) == ORIGINAL_SHA256[name] for name in INPUT_NAMES)
    expected_exact = all(digest(out[name]) == TESTED_M2_SHA256[name] for name in OUTPUT_NAMES)
    # With my original inputs, the output must be identical to the working M2 files.
    if input_exact and not expected_exact:
        raise BuildError('Known-original regression mismatch against the working M2 output SHA256 values')
    report = {
        'tool': 'GT3 Paint Shop Portable', 'tool_version': VERSION,
        'feature': 'M2 preview only (factory colours; no permanent Apply Paint)',
        'input_known_original_4_of_4': input_exact,
        'output_matches_user_tested_M2_3_of_3': expected_exact if input_exact else None,
        'input_hashes': {name: digest(originals[name]) for name in INPUT_NAMES},
        'output_hashes': {name: digest(out[name]) for name in OUTPUT_NAMES},
        'output_sizes': {name: len(out[name]) for name in OUTPUT_NAMES},
        'page': PAGE,
        'gt_auto_entry': 'pt -> at/ps/ps',
        'mbl_main_before': len(old_mbl[2]), 'mbl_main_after': len(mbl[2]),
        'pmb601_pages_unchanged_except_home': len(original_pages['gt_mode601.pmb'][2]) - 1,
        'pmb610_pages_before': len(original_pages['gt_mode610.pmb'][2]),
        'pmb610_pages_after': len(p610[2]),
        'pmb610_texture_entries_preserved': len(images610),
        'functions_on_new_page': sorted(functions),
        'mbl_and_pmb_top_level_raw_lexical_order': True,
        'binary_roundtrip_test': 'PASS',
        'purchase_actions_added': False,
        'elf_changed': False, 'database_changed': False, 'fbi_changed': False,
        'tested_variant_note': 'User-confirmed: GT3 PAL SCES-50294, BR menu, external-ELF HostFS setup. Other builds/locales require in-game verification.',
        'input_order_note': 'MAIN tables are sorted by raw name. The GT Auto service links keep the tested order: gr, st, sl, pt (wash, oil, wheels, paint).',
    }
    return out, report


def find_inputs(source: Path, locale: str) -> tuple[dict[str, Path], Path | None]:
    source = source.resolve()
    if not source.is_dir():
        raise BuildError(f'Source directory not found: {source}')
    candidates = [source, source / 'data']
    result = []
    for root in candidates:
        nested = {'gt_mode.mbl': root / 'menu' / 'gt_mode.mbl'}
        nested.update({name: root / 'menu' / locale / name for name in INPUT_NAMES if name.endswith('.pmb')})
        if all(p.is_file() for p in nested.values()):
            result.append((nested, root))
    flat = {name: source / name for name in INPUT_NAMES}
    if all(p.is_file() for p in flat.values()):
        result.append((flat, None))
    if len(result) != 1:
        if not result:
            expected = source / 'menu' / locale
            raise BuildError('Could not find four original files. Provide either a flat directory with '
                             'gt_mode.mbl, gt_mode601.pmb, gt_mode610.pmb, gt_mode310.pmb; '
                             f'or a GT3 game data root containing menu/gt_mode.mbl and {expected.name}/gt_mode*.pmb.')
        raise BuildError('Ambiguous input roots: provide the exact data directory or a flat input directory')
    return result[0]


def safe_outdir(output: Path, source: Path):
    resolved = output.resolve()
    # Write to a separate folder. Keep the original extracted menus untouched.
    if resolved == source.resolve() or resolved.is_relative_to(source.resolve()):
        raise BuildError('Refusing to write inside the source tree. Choose a separate --output directory.')
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise BuildError('Output must be a new or empty directory; no existing files will be overwritten')


def write_outputs(output: Path, files: dict[str, bytes], report: dict, locale: str):
    # All checks happen before this. Only write into a new, empty folder.
    output.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, raw in files.items():
        relative = Path('menu') / (name if name.endswith('.mbl') else str(Path(locale) / name))
        final = output / relative
        final.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.' + name + '.', suffix='.part', dir=final.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if final.exists():
                raise BuildError(f'Output file already exists: {final}')
            os.replace(temp, final)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        if digest(final.read_bytes()) != digest(raw):
            raise BuildError(f'Written file SHA256 check failed: {final}')
        manifest[name] = relative.as_posix()
    report['output_relative_paths'] = manifest
    (output / 'build_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Create the original successful GT3 GT Auto Paint Shop M2 preview from your own MBL/PMB files.'
    )
    parser.add_argument('--source', type=Path, required=True,
                        help='Extracted game data root (with menu/) or a flat folder holding the four input files')
    parser.add_argument('--output', type=Path, help='New/empty output folder. Required unless --check-only')
    parser.add_argument('--locale', default='BR', help='GT3 localization folder, default BR; other locales untested')
    parser.add_argument('--check-only', action='store_true', help='Validate inputs and calculate outputs in memory, without writing')
    parser.add_argument('--version', action='version', version=f'GT3 Paint Shop Portable v{VERSION}')
    args = parser.parse_args(argv)
    if not args.locale.isalnum() or len(args.locale) > 8:
        raise BuildError('Invalid localization folder: use BR for the tested configuration')
    if not args.check_only and args.output is None:
        parser.error('--output is required unless --check-only is specified')
    paths, data_root = find_inputs(args.source, args.locale)
    originals = {}
    for name, path in paths.items():
        if path.stat().st_size > MAX_BINARY:
            raise BuildError(f'Input is too large: {path}')
        originals[name] = path.read_bytes()
    outputs, report = build(originals)
    print('PASS: four original MBL/PMB inputs parsed and round-tripped byte-exactly')
    print(f'PASS: created {PAGE} and linked it from GT Auto')
    print('PASS: top-level MBL/PMB MAIN ordering, pointers, image indices, and original pages checked')
    if report['input_known_original_4_of_4']:
        print('PASS: all four inputs match the original GT3 BR test files')
        print('PASS: all three generated outputs SHA256-match the previously tested Paint Shop M2 files')
    else:
        print('WARNING: these source hashes differ from the verified PAL BR originals; runtime compatibility is NOT established')
    if data_root is not None:
        fbi = data_root / 'menu' / 'showroom_garage.fbi'
        if not fbi.is_file():
            print('WARNING: showroom_garage.fbi not found at the expected extracted-game location')
        # The game already loads this camera preset. We don't need to touch it.
    if args.check_only:
        print('CHECK ONLY: no files were written')
    else:
        safe_outdir(args.output, args.source)
        write_outputs(args.output, outputs, report, args.locale)
        print(f'Generated 3 files under {args.output.resolve()}')
        print('No ELF, database, FBI, PNACH, ISO, or game installation was modified.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (BuildError, ValueError, KeyError, IndexError, OSError, struct.error, TypeError, RecursionError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
