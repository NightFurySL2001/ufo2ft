import enum
import logging
import re
from io import BytesIO

from fontTools.cffLib.CFFToCFF2 import convertCFFToCFF2
from fontTools.ttLib import TTFont
from fontTools.ttLib.standardGlyphOrder import standardGlyphOrder

from ufo2ft.constants import (
    GLYPHS_DONT_USE_PRODUCTION_NAMES,
    KEEP_GLYPH_NAMES,
    USE_PRODUCTION_NAMES,
    CFFOptimization,
)

logger = logging.getLogger(__name__)


class CFFVersion(enum.IntEnum):
    CFF = 1
    CFF2 = 2


class PostProcessor:
    """Does some post-processing operations on a compiled OpenType font, using
    info from the source UFO where necessary.
    """

    GLYPH_NAME_INVALID_CHARS = re.compile("[^0-9a-zA-Z_.]")
    MAX_GLYPH_NAME_LENGTH = 63

    class SubroutinizerBackend(enum.Enum):
        COMPREFFOR = "compreffor"
        CFFSUBR = "cffsubr"

    # can override by passing explicit subroutinizer parameter to process method
    DEFAULT_SUBROUTINIZER_FOR_CFF_VERSION = {
        1: SubroutinizerBackend.CFFSUBR,
        2: SubroutinizerBackend.CFFSUBR,
    }

    def __init__(self, otf, ufo, glyphSet=None, info=None):
        self.ufo = ufo
        self.glyphSet = glyphSet if glyphSet is not None else ufo
        self.info = info
        self.otf = otf

        self._postscriptNames = ufo.lib.get("public.postscriptNames")

    def process(
        self,
        useProductionNames=None,
        optimizeCFF=True,
        cffVersion=None,
        subroutinizer=None,
    ):
        """
        useProductionNames (Optional[bool]):
          By default, when value is None, this will rename glyphs using the
          'public.postscriptNames' in then UFO lib. If the mapping is not
          present, no glyph names are renamed.

          When useProductionNames is None and the UFO lib contains the plist bool key
          "com.github.googlei18n.ufo2ft.keepGlyphNames" set to False, then the 'post'
          table is set to format 3.0 and glyph names are dropped altogether from the
          font, saving a few KBs. Note that this only works for TTF and CFF2 flavored
          fonts. We currently do not support dropping glyph names from CFF 1.0 fonts.
          When the keepGlyphNames lib key is missing or set to True, the glyph names
          will be stored in 'post' table format 2.0 for TTF and CFF2 fonts, or in the
          CFF charset.

          If useProductionNames is False, no glyphs are renamed whether or not the
          'public.postscriptNames' mapping is present.
          If the value is True, but no 'public.postscriptNames' are present,
          then uniXXXX names are generated from the glyphs' unicode.

          The 'com.github.googlei18n.ufo2ft.useProductionNames' key can be set
          in the UFO lib to control this parameter (plist boolean value).

          For legacy reasons, an alias key (with an inverted meaning) is also
          supported: "com.schriftgestaltung.Don't use Production Names";
          when this is present if the UFO lib and is set to True, this is
          equivalent to 'useProductionNames' set to False.

        optimizeCFF (bool | CFFOptimization):
          If True or >= CFFOptimization.SUBROUTINIZE, subroubtinize CFF or CFF2 table
          (if present).

        cffVersion (Optiona[int]):
          The output CFF format, choose between 1 or 2. By default, it's the same as
          as the input OTF's CFF or CFF2 table, if any. Ignored for TTFs.

        subroutinizer (Optional[str]):
          The name of the library to use for compressing CFF charstrings, if optimizeCFF
          is True and CFF or CFF2 table is present. Choose between "cffsubr" or
          "compreffor". By default "cffsubr" is used for both CFF 1 and CFF 2.
          NOTE: compreffor currently doesn't support input fonts with CFF2 table.
        """
        if self._get_cff_version(self.otf):
            if not isinstance(optimizeCFF, bool):
                optimizeCFF = optimizeCFF >= CFFOptimization.SUBROUTINIZE
            self.process_cff(
                optimizeCFF=optimizeCFF,
                cffVersion=cffVersion,
                subroutinizer=subroutinizer,
            )

        self.process_glyph_names(useProductionNames)

        if self.info:
            self.apply_fontinfo()

        return self.otf

    def process_cff(self, *, optimizeCFF=True, cffVersion=None, subroutinizer=None):
        cffInputVersion = self._get_cff_version(self.otf)
        if not cffInputVersion:
            raise ValueError("Missing required 'CFF ' or 'CFF2' table")

        if cffVersion is None:
            cffOutputVersion = cffInputVersion
        else:
            cffOutputVersion = CFFVersion(cffVersion)

        if optimizeCFF:
            if subroutinizer is None:
                backend = self.DEFAULT_SUBROUTINIZER_FOR_CFF_VERSION[cffOutputVersion]
            else:
                backend = self.SubroutinizerBackend(subroutinizer)
            self._subroutinize(backend, self.otf, cffOutputVersion)

        elif cffInputVersion != cffOutputVersion:
            if (
                cffInputVersion == CFFVersion.CFF
                and cffOutputVersion == CFFVersion.CFF2
            ):
                logger.info("Converting CFF table to CFF2")
                convertCFFToCFF2(self.otf)
            else:
                raise NotImplementedError(
                    "Unsupported CFF conversion {cffInputVersion} => {cffOutputVersion}"
                )

    def process_glyph_names(self, useProductionNames=None):
        if useProductionNames is None:
            keepGlyphNames = self.ufo.lib.get(KEEP_GLYPH_NAMES, True)
            useProductionNames = self.ufo.lib.get(
                USE_PRODUCTION_NAMES,
                not self.ufo.lib.get(GLYPHS_DONT_USE_PRODUCTION_NAMES)
                and self._postscriptNames is not None,
            )
        else:
            keepGlyphNames = True

        if keepGlyphNames:
            if "CFF " not in self.otf:
                self.set_post_table_format(self.otf, 2.0)

            if useProductionNames:
                logger.info("Renaming glyphs to final production names")
                # We need to reload the font *before* renaming glyphs, since various
                # tables may have been build/loaded using the original glyph names.
                # After reloading, we can immediately set a new glyph order and update
                # the tables (post or CFF) that stores the new postcript names; any
                # other tables that get loaded subsequently will use the new glyph names.
                self.otf = _reloadFont(self.otf)
                self._rename_glyphs_from_ufo()

        else:
            if "CFF " in self.otf:
                logger.warning(
                    "Dropping glyph names from CFF 1.0 is currently unsupported"
                )
            else:
                # To drop glyph names from TTF or CFF2, we must reload the font *after*
                # setting the post format to 3.0, since other tables may still use
                # the old glyph names.
                self.set_post_table_format(self.otf, 3.0)
                self.otf = _reloadFont(self.otf)

    def _rename_glyphs_from_ufo(self):
        """Rename glyphs using ufo.lib.public.postscriptNames in UFO."""
        rename_map = self._build_production_names()
        self.rename_glyphs(self.otf, rename_map)

    @staticmethod
    def rename_glyphs(otf, rename_map):
        newGlyphOrder = [rename_map.get(n, n) for n in otf.getGlyphOrder()]
        otf.setGlyphOrder(newGlyphOrder)

        if "post" in otf and otf["post"].formatType == 2.0:
            # annoyingly we need to update extraNames to match the new glyph order,
            # otherwise, if dumping the font to TTX directly before compiling first,
            # the post table will not contain the extraNames...
            otf["post"].extraNames = [
                g for g in newGlyphOrder if g not in standardGlyphOrder
            ]
            otf["post"].mapping = {}

        cff_tag = "CFF " if "CFF " in otf else "CFF2" if "CFF2" in otf else None
        if cff_tag == "CFF " or (cff_tag == "CFF2" and otf.isLoaded(cff_tag)):
            cff = otf[cff_tag].cff.topDictIndex[0]
            char_strings = cff.CharStrings.charStrings
            cff.CharStrings.charStrings = {
                rename_map.get(n, n): v for n, v in char_strings.items()
            }
            cff.charset = [rename_map.get(n, n) for n in cff.charset]

    def _build_production_names(self):
        seen = {}
        rename_map = {}
        for name in self.otf.getGlyphOrder():
            # Ignore glyphs that aren't in the source, as they are usually generated
            # and we lack information about them.
            if name not in self.glyphSet:
                continue
            prod_name = self._build_production_name(self.glyphSet[name])

            # strip invalid characters not allowed in postscript glyph names
            if name != prod_name:
                valid_name = self.GLYPH_NAME_INVALID_CHARS.sub("", prod_name)
                if len(valid_name) > self.MAX_GLYPH_NAME_LENGTH:
                    # if the length of the generated production name is too
                    # long, try to fall back to the original name
                    valid_name = self.GLYPH_NAME_INVALID_CHARS.sub("", name)
            else:
                valid_name = self.GLYPH_NAME_INVALID_CHARS.sub("", name)

            if len(valid_name) > self.MAX_GLYPH_NAME_LENGTH:
                logger.warning(
                    "glyph name length exceeds 63 characters: '%s'", valid_name
                )
            # add a suffix to make the production names unique
            rename_map[name] = self._unique_name(valid_name, seen)
        return rename_map

    @staticmethod
    def _unique_name(name, seen):
        """Append incremental '.N' suffix if glyph is a duplicate."""
        if name in seen:
            n = seen[name]
            while (name + ".%d" % n) in seen:
                n += 1
            seen[name] = n + 1
            name += ".%d" % n
        seen[name] = 1
        return name

    def _build_production_name(self, glyph):
        """Build a production name for a single glyph."""

        # use PostScript names from UFO lib if available
        if self._postscriptNames:
            production_name = self._postscriptNames.get(glyph.name)
            return production_name if production_name else glyph.name

        # use name derived from unicode value
        unicode_val = glyph.unicode
        if glyph.unicode is not None:
            return "{}{:04X}".format(
                "u" if unicode_val > 0xFFFF else "uni", unicode_val
            )

        # use production name + last (non-script) suffix if possible
        parts = glyph.name.rsplit(".", 1)
        if len(parts) == 2 and parts[0] in self.glyphSet:
            return "{}.{}".format(
                self._build_production_name(self.glyphSet[parts[0]]),
                parts[1],
            )

        # use ligature name, making sure to look up components with suffixes
        parts = glyph.name.split(".", 1)
        if len(parts) == 2:
            liga_parts = ["{}.{}".format(n, parts[1]) for n in parts[0].split("_")]
        else:
            liga_parts = glyph.name.split("_")
        if len(liga_parts) > 1 and all(n in self.glyphSet for n in liga_parts):
            unicode_vals = [self.glyphSet[n].unicode for n in liga_parts]
            if all(v and v <= 0xFFFF for v in unicode_vals):
                return "uni" + "".join("%04X" % v for v in unicode_vals)
            return "_".join(
                self._build_production_name(self.glyphSet[n]) for n in liga_parts
            )

        return glyph.name

    @staticmethod
    def set_post_table_format(otf, formatType):
        if formatType not in (2.0, 3.0):
            raise NotImplementedError(formatType)

        post = otf.get("post")
        if post:
            if post.formatType != formatType:
                logger.info("Setting post.formatType = %s", formatType)
                post.formatType = formatType
            # we want to update extraNames list even if formatType is the same
            # so we don't have to reload the font
            if formatType == 2.0:
                post.extraNames = [
                    g for g in otf.getGlyphOrder() if g not in standardGlyphOrder
                ]
                post.mapping = {}
            else:
                for attr in ("extraNames", "mapping"):
                    if hasattr(post, attr):
                        delattr(post, attr)
                post.glyphOrder = None

    @staticmethod
    def _get_cff_version(otf):
        if "CFF " in otf:
            return CFFVersion.CFF
        elif "CFF2" in otf:
            return CFFVersion.CFF2
        else:
            return None

    @classmethod
    def _subroutinize(cls, backend, otf, cffVersion):
        subroutinize = getattr(cls, f"_subroutinize_with_{backend.value}")
        subroutinize(otf, cffVersion)

    @classmethod
    def _subroutinize_with_compreffor(cls, otf, cffVersion):
        from compreffor import compress

        if cls._get_cff_version(otf) != CFFVersion.CFF or cffVersion != CFFVersion.CFF:
            raise NotImplementedError(
                "Only 'CFF ' 1.0 is supported by compreffor; try using cffsubr"
            )

        logger.info("Subroutinizing CFF table with compreffor")

        compress(otf)

    @classmethod
    def _subroutinize_with_cffsubr(cls, otf, cffVersion):
        import cffsubr

        cffInputVersion = cls._get_cff_version(otf)
        assert cffInputVersion is not None, "Missing required 'CFF ' or 'CFF2' table"

        msg = f"Subroutinizing {cffInputVersion.name} table with cffsubr"
        if cffInputVersion != cffVersion:
            msg += f" (output format: {cffVersion.name})"
        logger.info(msg)

        return cffsubr.subroutinize(otf, cff_version=cffVersion, keep_glyph_names=False)

    def apply_fontinfo(self):
        """Apply the fontinfo data from the DesignSpace variable-font's lib to
        the compiled font."""
        from ufo2ft.infoCompiler import InfoCompiler

        weightClass = self.otf["OS/2"].usWeightClass
        widthClass = self.otf["OS/2"].usWidthClass
        italicAngle = self.otf["post"].italicAngle

        logger.info("Applying variable-font info from DesignSpace lib")

        compiler = InfoCompiler(self.otf, self.ufo, self.info)
        compiler.compile()

        # restore the previous weight/width/slant values unless the 'public.fontInfo'
        # key explicitly overrode them. fontTools.varLib.build() may have updated them
        # to match the VF's default location (as recommended by the OT spec), and the
        # InfoCompiler (which rebuilds OS/2 and post) risks overwriting them:
        # https://github.com/googlefonts/ufo2ft/issues/914
        if "openTypeOSWeightClass" not in self.info:
            self.otf["OS/2"].usWeightClass = weightClass
        if "openTypeOSWidthClass" not in self.info:
            self.otf["OS/2"].usWidthClass = widthClass
        if "italicAngle" not in self.info:
            self.otf["post"].italicAngle = italicAngle


def _reloadFont(font: TTFont) -> TTFont:
    """Recompile a font to arrive at the final internal layout."""
    stream = BytesIO()
    font.save(stream)
    stream.seek(0)
    # keep the same Config (constructor will make a copy)
    return TTFont(stream, cfg=font.cfg)
