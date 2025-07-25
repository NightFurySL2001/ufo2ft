import itertools
import re
from collections import OrderedDict, defaultdict
from functools import partial
from typing import Dict, Optional, Set, Tuple

from ufo2ft.constants import (
    ANCHOR_LIB_GPOS_CONTEXT_KEY,
    INDIC_SCRIPTS,
    OBJECT_LIBS_KEY,
    USE_SCRIPTS,
)
from ufo2ft.featureWriters import BaseFeatureWriter, ast
from ufo2ft.util import (
    classifyGlyphs,
    otRoundIgnoringVariable,
    unicodeInScripts,
    unicodeScriptExtensions,
)


class AbstractMarkPos:
    """Object containing all the mark attachments for glyph 'name'.
    The 'marks' is a list of NamedAnchor objects.
    Provides methods to filter marks given some callable, and convert
    itself to feaLib AST 'pos' statements for mark2base, mark2liga and
    mark2mark lookups.
    """

    Statement = None

    def __init__(self, name, marks):
        self.name = name
        self.marks = marks

    def _filterMarks(self, include):
        return [anchor for anchor in self.marks if include(anchor)]

    def _marksAsAST(self):
        return [
            (
                ast.Anchor(
                    x=otRoundIgnoringVariable(anchor.x),
                    y=otRoundIgnoringVariable(anchor.y),
                ),
                anchor.markClass,
            )
            for anchor in sorted(self.marks, key=lambda a: a.name)
        ]

    def asAST(self):
        marks = self._marksAsAST()
        return self.Statement(ast.GlyphName(self.name), marks)

    def __str__(self):
        return self.asAST().asFea()  # pragma: no cover

    def filter(self, include):
        marks = self._filterMarks(include)
        return self.__class__(self.name, marks) if any(marks) else None

    def getMarkGlyphToMarkClasses(self):
        """Return a list of pairs (markGlyph, markClasses)."""
        markGlyphToMarkClasses = defaultdict(set)
        for namedAnchor in self.marks:
            for markGlyph in namedAnchor.markClass.glyphs:
                markGlyphToMarkClasses[markGlyph].add(namedAnchor.markClass.name)
        return markGlyphToMarkClasses.items()


class MarkToBasePos(AbstractMarkPos):
    Statement = ast.MarkBasePosStatement


class MarkToMarkPos(AbstractMarkPos):
    Statement = ast.MarkMarkPosStatement


class MarkToLigaPos(AbstractMarkPos):
    Statement = ast.MarkLigPosStatement

    def _filterMarks(self, include):
        return [
            [anchor for anchor in component if include(anchor)]
            for component in self.marks
        ]

    def _marksAsAST(self):
        return [
            [
                (
                    ast.Anchor(
                        x=otRoundIgnoringVariable(anchor.x),
                        y=otRoundIgnoringVariable(anchor.y),
                    ),
                    anchor.markClass,
                )
                for anchor in sorted(component, key=lambda a: a.name)
            ]
            for component in self.marks
        ]

    def getMarkGlyphToMarkClasses(self):
        """Return a list of pairs (markGlyph, markClasses)."""
        markGlyphToMarkClasses = defaultdict(set)
        for component in self.marks:
            for namedAnchor in component:
                for markGlyph in namedAnchor.markClass.glyphs:
                    markGlyphToMarkClasses[markGlyph].add(namedAnchor.markClass.name)
        return markGlyphToMarkClasses.items()


MARK_PREFIX = LIGA_SEPARATOR = "_"
LIGA_NUM_RE = re.compile(r".*?(\d+)$")


def parseAnchorName(
    anchorName,
    markPrefix=MARK_PREFIX,
    ligaSeparator=LIGA_SEPARATOR,
    ligaNumRE=LIGA_NUM_RE,
    ignoreRE=None,
    libData=None,
):
    """Parse anchor name and return a tuple that specifies:
    1) whether the anchor is a "mark" anchor (bool);
    2) the "key" name of the anchor, i.e. the name after stripping all the
       prefixes and suffixes, which identifies the class it belongs to (str);
    3) An optional number (int), starting from 1, which identifies that index
       of the ligature component the anchor refers to.

    The 'ignoreRE' argument is an optional regex pattern (str) identifying
    sub-strings in the anchor name that should be ignored when parsing the
    three elements above.
    """
    number = None
    isContextual = False
    if ignoreRE is not None:
        anchorName = re.sub(ignoreRE, "", anchorName)

    if anchorName[0] == "*" and libData and ANCHOR_LIB_GPOS_CONTEXT_KEY in libData:
        isContextual = True
        anchorName = anchorName[1:]
        anchorName = re.sub(r"\..*", "", anchorName)

    m = ligaNumRE.match(anchorName)
    if not m:
        key = anchorName
    else:
        number = m.group(1)
        key = anchorName.rstrip(number)
        separator = ligaSeparator
        if key.endswith(separator):
            assert separator
            key = key[: -len(separator)]
            number = int(number)
        else:
            # not a valid ligature anchor name
            key = anchorName
            number = None

    if anchorName.startswith(markPrefix) and key:
        if number is not None:
            raise ValueError("mark anchor cannot be numbered: %r" % anchorName)
        isMark = True
        key = key[len(markPrefix) :]
        if not key:
            raise ValueError("mark anchor key is nil: %r" % anchorName)
    else:
        isMark = False

    isIgnorable = key and not key[0].isalpha()

    return isMark, key, number, isContextual, isIgnorable


class NamedAnchor:
    """A position with a name, and an associated markClass."""

    __slots__ = (
        "name",
        "x",
        "y",
        "isMark",
        "key",
        "number",
        "markClass",
        "isContextual",
        "isIgnorable",
        "libData",
    )

    # subclasses can customize these to use different anchor naming schemes
    markPrefix = MARK_PREFIX
    ignoreRE = None
    ligaSeparator = LIGA_SEPARATOR
    ligaNumRE = LIGA_NUM_RE

    def __init__(self, name, x, y, markClass=None, libData=None):
        self.name = name
        self.x = x
        self.y = y
        isMark, key, number, isContextual, isIgnorable = parseAnchorName(
            name,
            markPrefix=self.markPrefix,
            ligaSeparator=self.ligaSeparator,
            ligaNumRE=self.ligaNumRE,
            ignoreRE=self.ignoreRE,
            libData=libData,
        )
        if number is not None:
            if number < 1:
                raise ValueError("ligature component indexes must start from 1")
        else:
            assert key, name
        self.isMark = isMark
        self.key = key
        self.number = number
        self.markClass = markClass
        self.isContextual = isContextual
        self.isIgnorable = isIgnorable
        self.libData = libData

    @property
    def markAnchorName(self):
        return self.markPrefix + self.key

    def __repr__(self):
        items = ("{}={!r}".format(k, getattr(self, k)) for k in ("name", "x", "y"))
        return "%s(%s)" % (type(self).__name__, ", ".join(items))


def colorGraph(adjacency):
    """Color the graph defined by the provided adjacency lists.
    The input is a dict of iterables. Each entry of the dict is one vertex,
    and the value is a list of neighbours of that vertex.
    The input graph is expected to be undirected and the input should reflect
    that (have symmetric adjacency for A -> B and B -> A).
    Vertices that don't have neighbours should still be present in the input.

    The output is a list of lists, each list being one color assignment,
    and its members being vertices.
    """
    # Basic implementation
    # https://en.wikipedia.org/wiki/Greedy_coloring
    colors = dict()
    # Sorted for reproducibility, probably not the optimal vertex order
    for node in sorted(adjacency):
        usedNeighbourColors = {
            colors[neighbour] for neighbour in adjacency[node] if neighbour in colors
        }
        colors[node] = firstAvailable(usedNeighbourColors)
    groups = defaultdict(list)
    for node, color in colors.items():
        groups[color].append(node)
    return list(groups.values())


def firstAvailable(colorSet):
    """Return smallest non-negative integer not in the given set of colors."""
    count = 0
    while True:
        if count not in colorSet:
            return count
        count += 1


class MarkFeatureWriter(BaseFeatureWriter):
    """Generates a mark, mkmk, abvm and blwm features based on glyph anchors.

    The default mode is 'skip': i.e. if any of the supported features is
    already present in the feature file, it is not generated again.

    The optional 'append' mode will add extra lookups to already existing
    features, if any. New markClass definitions with unique names are
    generated when the mark anchors from UFO glyphs are different from those
    already defined in the feature file, otherwise the existing markClass
    definitions are reused in the newly appended lookups.

    Anchors prefixed with "_" are considered mark anchors; any glyph
    containing those is as such considered a mark glyph, thus added to
    markClass definitions, and in mark-to-mark lookups (if the glyph also
    contains other non-underscore-prefixed anchors).

    Anchors suffixed with a number, e.g. "top_1", "bottom_2", etc., are used
    for ligature glyphs. The number refers to the index (counting from 1) of
    the ligature component where the mark is meant to be attached.

    It is possible that a ligature component has no marks defined, in which
    case one can have an anchor with an empty name and only the number (e.g.
    '_3'), which is encoded as '<anchor NULL>' in the generated 'pos ligature'
    statement.

    If the glyph set contains glyphs whose unicode codepoint's script extension
    property intersects with one of the scripts which are processed by the Indic,
    USE, or Khmer complex shapers, then the "abvm" and "blwm" features are also
    generated for those glyphs, as well as for alternate glyphs only accessible
    via GSUB substitutions.

    The "abvm" (above-base marks) and "blwm" (below-base marks) features
    include all mark2base, mark2liga and mark2mark attachments for Indic/USE/Khmer
    glyphs containing anchors from predefined lists of "above" and "below" anchor
    names (see below). If these glyphs contain anchors with names not in those
    lists, the anchors' vertical position relative to the half of the UPEM
    square is used to decide whether they are considered above or below.

    If the `quantization` argument is given in the filter options, the resulting
    anchors are rounded to the nearest multiple of the quantization value.

    If `groupMarkClases=True`, mark-to-base or mark-to-ligature attachments that
    reference non-overlapping mark classes will get grouped in the same lookup; and
    if a mark glyph is in more than one mark class, additional lookups will be generated
    for those as required. NOTE: this was the default behavior until ufo2ft 2.33.4.
    The current default behavior was simplified to match other font editors and
    we now build as many mark-to-base and mark-to-liga lookups as there
    are mark classes, and lookups are sorted alphabetically by the mark class
    name so the more specific ('top.alt' instead 'top') would be applied last and
    wins in case when the same base or ligature glyph can attach to the same mark
    through multiple mark classes.
    https://github.com/googlefonts/ufo2ft/issues/591
    """

    options = dict(quantization=1, groupMarkClasses=False)

    tableTag = "GPOS"
    features = frozenset(["mark", "mkmk", "abvm", "blwm"])

    # subclasses may override this to use different anchor naming schemes
    NamedAnchor = NamedAnchor

    # @MC_top, @MC_bottom, etc.
    markClassPrefix = "MC"

    abvmAnchorNames = {
        "top",
        "topleft",
        "topright",
        "candra",
        "bindu",
        "candrabindu",
        "imatra",
    }
    blwmAnchorNames = {"bottom", "bottomleft", "bottomright", "nukta"}

    scriptsUsingAbvm = set(INDIC_SCRIPTS + USE_SCRIPTS + ["Khmr"])

    # Glyphs moves "_bottom" and "_top" (if present) to the top of
    # the list and then picks the first to use in the mark feature.
    # https://github.com/googlei18n/noto-source/issues/122#issuecomment-403952188
    anchorSortKey = {"_bottom": -2, "_top": -1}

    def setContext(self, font, feaFile, compiler=None):
        ctx = super().setContext(font, feaFile, compiler=compiler)
        ctx.gdefClasses = self.getGDEFGlyphClasses()
        ctx.anchorLists = self._getAnchorLists()
        ctx.anchorPairs = self._getAnchorPairs()
        ctx.feaScripts = set(ast.getScriptLanguageSystems(feaFile).keys())

    def shouldContinue(self):
        if not self.context.anchorPairs:
            self.log.debug("No mark-attaching anchors found; skipped")
            return False
        return super().shouldContinue()

    def _getAnchorLists(self):
        gdefClasses = self.context.gdefClasses
        if gdefClasses.base is not None:
            # only include the glyphs listed in the GDEF.GlyphClassDef groups
            include = gdefClasses.base | gdefClasses.ligature | gdefClasses.mark
        else:
            # no GDEF table defined in feature file, include all glyphs
            include = None
        result = OrderedDict()
        for glyphName, glyph in self.getOrderedGlyphSet().items():
            if include is not None and glyphName not in include:
                continue
            anchorDict = OrderedDict()
            objectLibs = glyph.lib.get(OBJECT_LIBS_KEY)
            for anchor in glyph.anchors:
                anchorName = anchor.name
                if not anchorName:
                    self.log.warning(
                        "unnamed anchor discarded in glyph '%s'", glyphName
                    )
                    continue
                if anchorName in anchorDict:
                    self.log.warning(
                        "duplicate anchor '%s' in glyph '%s'", anchorName, glyphName
                    )
                x, y = self._getAnchor(glyphName, anchorName, anchor=anchor)
                libData = None
                if anchor.identifier and objectLibs:
                    libData = objectLibs.get(anchor.identifier)
                a = self.NamedAnchor(name=anchorName, x=x, y=y, libData=libData)
                if a.isIgnorable:
                    continue
                anchorDict[anchorName] = a
            if anchorDict:
                result[glyphName] = list(anchorDict.values())
        return result

    def _getAnchorPairs(self):
        markAnchorNames = set()
        for anchors in self.context.anchorLists.values():
            markAnchorNames.update(a.name for a in anchors if a.isMark)
        anchorPairs = {}
        for anchors in self.context.anchorLists.values():
            for anchor in anchors:
                if anchor.isMark:
                    continue
                markAnchorName = anchor.markAnchorName
                if markAnchorName in markAnchorNames:
                    anchorPairs[anchor.name] = markAnchorName
        return anchorPairs

    def _pruneUnusedAnchors(self):
        baseAnchorNames = set(self.context.anchorPairs.keys())
        markAnchorNames = set(self.context.anchorPairs.values())
        attachingAnchorNames = baseAnchorNames | markAnchorNames
        for glyphName, anchors in list(self.context.anchorLists.items()):
            for anchor in list(anchors):
                if anchor.name not in attachingAnchorNames and anchor.key:
                    anchors.remove(anchor)
            if not anchors:
                del self.context.anchorLists[glyphName]

    def _groupMarkGlyphsByAnchor(self):
        gdefMarks = self.context.gdefClasses.mark
        markAnchorNames = set(self.context.anchorPairs.values())
        markGlyphNames = set()
        groups = {}
        for glyphName, anchors in self.context.anchorLists.items():
            # if the feature file has a GDEF table with GlyphClassDef defined,
            # only include mark glyphs that are referenced in there, otherwise
            # include any glyphs that contain an "_" prefixed anchor.
            if gdefMarks is not None and glyphName not in gdefMarks:
                continue
            markAnchors = [a for a in anchors if a.name in markAnchorNames]
            if not markAnchors:
                continue
            # Use all mark anchors. The rest of the algorithm will make sure
            # that the generated lookups will not have overlapping mark classes.
            for anchor in markAnchors:
                group = groups.setdefault(anchor.name, OrderedDict())
                assert glyphName not in group
                group[glyphName] = anchor
            markGlyphNames.add(glyphName)
        self.context.markGlyphNames = markGlyphNames
        return groups

    def _makeMarkClassDefinitions(self):
        markGlyphSets = self._groupMarkGlyphsByAnchor()
        currentClasses = self.context.feaFile.markClasses
        allMarkClasses = self.context.markClasses = {}
        classPrefix = self.markClassPrefix
        newDefs = []
        for markAnchorName, glyphAnchorPairs in sorted(markGlyphSets.items()):
            className = ast.makeFeaClassName(classPrefix + markAnchorName)
            for glyphName, anchor in glyphAnchorPairs.items():
                mcd = self._defineMarkClass(
                    glyphName, anchor.x, anchor.y, className, currentClasses
                )
                if mcd is not None:
                    newDefs.append(mcd)
                    # this may be different because of name clashes
                    className = mcd.markClass.name
                allMarkClasses[anchor.key] = currentClasses[className]
        return newDefs

    def _defineMarkClass(self, glyphName, x, y, className, markClasses):
        anchor = ast.Anchor(x=otRoundIgnoringVariable(x), y=otRoundIgnoringVariable(y))
        markClass = markClasses.get(className)
        if markClass is None:
            markClass = ast.MarkClass(className)
            markClasses[className] = markClass
        else:
            if glyphName in markClass.glyphs:
                mcdef = markClass.glyphs[glyphName]
                if self._anchorsAreEqual(anchor, mcdef.anchor):
                    self.log.debug(
                        "Glyph %s already defined in markClass @%s",
                        glyphName,
                        className,
                    )
                    return None
                else:
                    # same mark glyph defined with different anchors for the
                    # same markClass; make a new unique markClass definition
                    newClassName = ast.makeFeaClassName(className, markClasses)
                    markClass = ast.MarkClass(newClassName)
                    markClasses[newClassName] = markClass
        glyphName = ast.GlyphName(glyphName)
        mcdef = ast.MarkClassDefinition(markClass, anchor, glyphName)
        markClass.addDefinition(mcdef)
        return mcdef

    @staticmethod
    def _anchorsAreEqual(a1, a2):
        # TODO add __eq__ to feaLib AST objects?
        return all(
            getattr(a1, attr) == getattr(a2, attr)
            for attr in ("x", "y", "contourpoint", "xDeviceTable", "yDeviceTable")
        )

    def _setBaseAnchorMarkClasses(self):
        markClasses = self.context.markClasses
        for anchors in self.context.anchorLists.values():
            for anchor in anchors:
                if anchor.isMark or not anchor.key or anchor.key not in markClasses:
                    continue
                anchor.markClass = markClasses[anchor.key]

    def _groupMarkClasses(self, markGlyphToMarkClasses):
        # To compute the number of lookups that we need to build, we want
        # the minimum number of lookups such that, whenever a mark glyph
        # belongs to several mark classes, these classes are not in the same
        # lookup. A trivial solution is to make 1 lookup per mark class
        # but that's a bit wasteful, we might be able to do better by grouping
        # mark classes that do not conflict.
        # This is a graph coloring problem: the graph nodes are mark classes,
        # edges are between classes that would conflict and the colors are
        # the lookups in which they can go.
        adjacency = {
            # We'll get the same markClass several times in the dict
            # comprehension below but it's ok, only one will be kept.
            markClass: set()
            for markClasses in markGlyphToMarkClasses.values()
            for markClass in markClasses
        }
        for _markGlyph, markClasses in markGlyphToMarkClasses.items():
            for markClass, other in itertools.combinations(markClasses, 2):
                adjacency[markClass].add(other)
                adjacency[other].add(markClass)
        colorGroups = colorGraph(adjacency)
        # Sort the groups, because the group that contains MC_top or MC_bottom
        # needs to go to the end (as specified in self.anchorSortKey) so that
        # they are applied last and "win" in case of conflict.
        # We also sort alphabetically for reproducibility, both within each
        # group and between groups.
        return sorted(
            [sorted(group) for group in colorGroups],
            key=lambda group: (
                # The first part sorts _top and _bottom at the end.
                # There's a minus sign in front of the min because the original
                # self.anchorSortKey was designed to put the _top and _bottom
                # at the start (and now we want them at the end).
                -min(
                    # Remove the MC prefix because that's how the mark classes
                    # are looking at this stage (the original
                    # self.anchorSortKey was applied at a different stage of
                    # the algorithm, on anchors instead of mark classes)
                    self.anchorSortKey.get(self._removeClassPrefix(markClass), 0)
                    for markClass in group
                ),
                # Second part of the tuple sorts the groups lexicographically
                group,
            ),
        )

    def _removeClassPrefix(self, markClass):
        assert markClass.startswith(self.markClassPrefix)
        return markClass[len(self.markClassPrefix) :]

    def _groupAttachments(self, attachments):
        """Group the given attachments so that no group contains conflicting
        anchor classes for the same glyph.
        """
        # Idea for mark2base:
        #   attachments is a list of mark to base pairs, linked together through
        #   an anchor name We have to put them into one or more lookups with the
        #   constraint that the same mark glyph cannot appear twice in the same
        #   lookup while using different anchor names.
        # Idea for mark2liga:
        #   attachments is a list of mark to liga positioning. Each links
        #   together a base ligature with several marks, through numbered anchor
        #   names.
        #   We have to put them into one or more lookups with the constraint that
        #   the same mark glyph cannot appear twice in the same lookup while
        #   using different anchor names.
        #   To do so, if a single attachment refers to to the same mark twice
        #   through different anchor names, we may have to split the attachment
        #   into two attachments, using null anchors instead of one or the other
        #   mark class in each split attachment.
        if self.options.groupMarkClasses:
            markGlyphToMarkClasses = defaultdict(set)
            for attachment in attachments:
                for markGlyph, markClasses in attachment.getMarkGlyphToMarkClasses():
                    markGlyphToMarkClasses[markGlyph].update(markClasses)
            groupedMarkClasses = self._groupMarkClasses(markGlyphToMarkClasses)
        else:
            # this will generate one lookup per mark class, and sort them
            # lexicographically by the anchor name, so the lookup for e.g.
            # '_top.alt01' will occur *after* the one for `_top` (the last wins) thus
            # allowing some degree of control on potentially ambiguous attachments
            # https://github.com/googlefonts/ufo2ft/issues/762
            # https://github.com/googlefonts/ufo2ft/issues/591
            groupedMarkClasses = [
                [markClass.name]
                for _, markClass in sorted(self.context.markClasses.items())
            ]
        lookups = []
        for markClasses in groupedMarkClasses:
            lookup = []
            # Filter existing attachments
            for attachment in attachments:
                # One attachment has one base glyph and many marks, each of
                # the class NamedAnchor. Each NamedAnchor has one markClass.
                # We keep the NamedAnchor if the markClass is allowed in the
                # current lookup.
                def include(anchor):
                    return anchor.markClass.name in markClasses  # noqa: B023

                filteredAttachment = attachment.filter(include)
                if filteredAttachment:
                    lookup.append(filteredAttachment)
            lookups.append(lookup)
        return lookups

    def _makeMarkToBaseAttachments(self):
        markGlyphNames = self.context.markGlyphNames
        baseClass = self.context.gdefClasses.base
        result = []
        for glyphName, anchors in self.context.anchorLists.items():
            # exclude mark glyphs, or glyphs not listed in GDEF Base
            if glyphName in markGlyphNames or (
                baseClass is not None and glyphName not in baseClass
            ):
                continue
            baseMarks = []
            for anchor in anchors:
                if anchor.markClass is None or anchor.number is not None:
                    # skip anchors for which no mark class is defined; also
                    # skip '_1', '_2', etc. suffixed anchors for this lookup
                    # type; these will be are added in the mark2liga lookup
                    continue
                if anchor.isContextual:
                    # skip contextual anchors. They are handled separately.
                    continue
                assert not anchor.isMark
                baseMarks.append(anchor)
            if not baseMarks:
                continue
            result.append(MarkToBasePos(glyphName, baseMarks))
        return result

    def _makeMarkToMarkAttachments(self):
        markGlyphNames = self.context.markGlyphNames
        # we make a dict of lists containing mkmk pos rules keyed by
        # anchor name, so we can create one mkmk lookup per markClass
        # each with different mark filtering sets.
        results = {}
        for glyphName, anchors in self.context.anchorLists.items():
            if glyphName not in markGlyphNames:
                continue
            for anchor in anchors:
                # skip anchors for which no mark class is defined
                if anchor.markClass is None or anchor.isMark:
                    continue
                if anchor.isContextual:
                    # skip contextual anchors. They are handled separately.
                    continue
                if anchor.number is not None:
                    self.log.warning(
                        "invalid ligature anchor '%s' in mark glyph '%s'; " "skipped",
                        anchor.name,
                        glyphName,
                    )
                    continue
                pos = MarkToMarkPos(glyphName, [anchor])
                results.setdefault(anchor.key, []).append(pos)
        return results

    def _makeMarkToLigaAttachments(self):
        markGlyphNames = self.context.markGlyphNames
        ligatureClass = self.context.gdefClasses.ligature
        result = []
        for glyphName, anchors in self.context.anchorLists.items():
            # exclude mark glyphs, or glyphs not listed in GDEF Ligature
            if glyphName in markGlyphNames or (
                ligatureClass is not None and glyphName not in ligatureClass
            ):
                continue
            componentAnchors = {}
            for anchor in anchors:
                if anchor.markClass is None and anchor.key:
                    # skip anchors for which no mark class is defined
                    continue
                assert not anchor.isMark
                number = anchor.number
                if number is None:
                    # we handled these in the mark2base lookup
                    continue
                if anchor.isContextual:
                    # skip contextual anchors. They are handled separately.
                    continue
                # unnamed anchors with only a number suffix "_1", "_2", etc.
                # are understood as the ligature component having <anchor NULL>
                if not anchor.key:
                    componentAnchors[number] = []
                else:
                    componentAnchors.setdefault(number, []).append(anchor)
            if not componentAnchors:
                continue
            ligatureMarks = []
            # ligature components are indexed from 1; any missing intermediate
            # anchor number means the component has <anchor NULL>
            for number in range(1, max(componentAnchors.keys()) + 1):
                ligatureMarks.append(componentAnchors.get(number, []))
            result.append(MarkToLigaPos(glyphName, ligatureMarks))
        return result

    def _makeContextualAttachments(
        self,
        baseClass: Optional[Set[str]],
        ligatureClass: Optional[Set[str]],
        markClass: Optional[Set[str]],
    ) -> Tuple[Dict[str, Tuple[str, NamedAnchor]], Dict[str, Tuple[str, NamedAnchor]]]:
        def includedOrNoClass(gdefClass: Optional[Set[str]], glyphName: str) -> bool:
            return glyphName in gdefClass if gdefClass is not None else True

        def includedInClass(gdefClass: Optional[Set[str]], glyphName: str) -> bool:
            return glyphName in gdefClass if gdefClass is not None else False

        markGlyphNames = self.context.markGlyphNames

        baseResult = defaultdict(list)
        ligatureResult = defaultdict(list)
        markResult = defaultdict(list)

        for glyphName, anchors in sorted(self.context.anchorLists.items()):
            for anchor in anchors:
                # Skip non-contextual anchors
                if not anchor.isContextual:
                    continue

                # Mark glyphs go to mkmk lookups
                if glyphName in markGlyphNames:
                    # skip anchors for which no mark class is defined
                    if anchor.markClass is None or anchor.isMark:
                        continue
                    if anchor.number is not None:
                        self.log.warning(
                            "invalid contextual ligature anchor '%s' in mark glyph '%s'; "
                            "skipped",
                            anchor.name,
                            glyphName,
                        )
                        continue
                    dest = markResult
                # See "after" truth table for what this logic hopes to achieve:
                # https://github.com/googlefonts/ufo2ft/pull/890#issuecomment-2498032081
                elif anchor.number is not None and includedOrNoClass(
                    ligatureClass, glyphName
                ):
                    dest = ligatureResult
                elif anchor.number is None and (
                    includedOrNoClass(baseClass, glyphName)
                    or includedInClass(ligatureClass, glyphName)
                ):
                    dest = baseResult
                else:
                    continue

                anchor_context = anchor.libData[ANCHOR_LIB_GPOS_CONTEXT_KEY].strip()

                if not anchor_context:
                    self.log.warning(
                        "contextual anchor '%s' in glyph '%s' has no context data; skipped",
                        anchor.name,
                        glyphName,
                    )
                    continue

                for context in anchor_context.splitlines():
                    context = context.strip().rstrip(";")
                    if not context:
                        continue
                    dest[context].append((glyphName, anchor))
        return baseResult, ligatureResult, markResult

    @staticmethod
    def _iterAttachments(attachments, include=None, marksFilter=None):
        for pos in attachments:
            if include is not None and not include(pos.name):
                continue
            if marksFilter is not None:
                pos = pos.filter(marksFilter)
                if pos is None:
                    continue
            yield pos

    def _makeMarkLookup(self, lookupName, attachments, include, marksFilter=None):
        statements = [
            pos.asAST()
            for pos in self._iterAttachments(attachments, include, marksFilter)
        ]
        if statements:
            lkp = ast.LookupBlock(lookupName)
            lkp.statements.extend(statements)
            return lkp

    def _makeMarkFilteringSetClass(self, lookupName, attachments, markClass, include):
        markGlyphs = (glyphName for glyphName in markClass.glyphs if include(glyphName))
        baseGlyphs = (
            pos.name for pos in attachments if pos.name not in markClass.glyphs
        )
        members = itertools.chain(markGlyphs, baseGlyphs)
        className = "MFS_%s" % lookupName
        return ast.makeGlyphClassDefinitions(
            {className: members}, feaFile=self.context.feaFile
        )[className]

    def _makeMarkToMarkLookup(
        self, anchorName, attachments, include, marksFilter=None, featureTag=None
    ):
        attachments = list(self._iterAttachments(attachments, include, marksFilter))
        if not attachments:
            return
        prefix = (featureTag + "_") if featureTag is not None else ""
        lookupName = f"{prefix}mark2mark_{anchorName}"
        filteringClass = self._makeMarkFilteringSetClass(
            lookupName,
            attachments,
            markClass=self.context.markClasses[anchorName],
            include=include,
        )
        lkp = ast.LookupBlock(lookupName)
        lkp.statements.append(filteringClass)
        lkp.statements.append(ast.makeLookupFlag(markFilteringSet=filteringClass))
        lkp.statements.extend(pos.asAST() for pos in attachments)
        return lkp

    def _makeMarkFeature(self, include):
        # First make the non-contextual lookups
        baseLkps = []
        for attachments in self.context.groupedMarkToBaseAttachments:
            i = len(baseLkps)
            lookup = self._makeMarkLookup(
                f"mark2base{'_' + str(i) if i > 0 else ''}", attachments, include
            )
            if lookup:
                baseLkps.append(lookup)
        ligaLkps = []
        for attachments in self.context.groupedMarkToLigaAttachments:
            i = len(ligaLkps)
            lookup = self._makeMarkLookup(
                f"mark2liga{'_' + str(i) if i > 0 else ''}", attachments, include
            )
            if lookup:
                ligaLkps.append(lookup)

        # Then make the contextual ones
        refLkps = []
        ctxLkps = {}
        # We sort the full context by longest first. This isn't perfect
        # but it gives us the best chance that more specific contexts
        # (typically longer) will take precedence over more general ones.
        for context, glyph_anchor_pair in sorted(
            self.context.contextualMarkToBaseAnchors.items(), key=lambda x: -len(x[0])
        ):
            # Group by anchor
            attachments = defaultdict(list)
            for glyphName, anchor in glyph_anchor_pair:
                attachments[anchor.key].append(MarkToBasePos(glyphName, [anchor]))
            self._makeContextualMarkLookup(
                attachments,
                context,
                refLkps,
                ctxLkps,
            )

        for context, glyph_anchor_pair in sorted(
            self.context.contextualMarkToLigaAnchors.items(), key=lambda x: -len(x[0])
        ):
            # Group by anchor
            attachments = defaultdict(list)
            for glyphName, anchor in glyph_anchor_pair:
                marks = [[]] * max(
                    a.number
                    for a in self.context.anchorLists[glyphName]
                    if a.key and a.number is not None
                )
                marks[anchor.number - 1] = [anchor]
                attachments[anchor.key].append(MarkToLigaPos(glyphName, marks))
            self._makeContextualMarkLookup(
                attachments,
                context,
                refLkps,
                ctxLkps,
            )

        ctxLkps = list(ctxLkps.values())
        if not baseLkps and not ligaLkps and not ctxLkps:
            return None, []

        feature = ast.FeatureBlock("mark")
        if ctxLkps:
            # When we have contextual lookups, we need to make sure that the
            # contextual and non-contextual lookups are in the right order
            # and we can’t use nested lookups inside the feature block for
            # the referenced lookups, so we put all lookups outside the feature
            # and use lookup references instead.
            # We should probably always do this, as nested lookups are full of
            # gotchas, but this will require updating many test expectations.
            lookups = baseLkps + ligaLkps + refLkps + ctxLkps
            for lookup in baseLkps + ligaLkps + ctxLkps:
                feature.statements.append(ast.LookupReferenceStatement(lookup))
        else:
            lookups = []
            for lookup in baseLkps + ligaLkps:
                feature.statements.append(lookup)
        return feature, lookups

    def _makeContextualMarkLookup(
        self,
        attachments,
        fullcontext,
        refLkps,
        ctxLkps,
        prefix="ContextualMark",
    ):
        for anchorKey, statements in attachments.items():
            # First make the contextual lookup
            if ";" in fullcontext:
                before, after = fullcontext.split(";")
            else:
                before, after = "", fullcontext
            after = after.strip()
            if before not in ctxLkps:
                ctxLkps[before] = ast.LookupBlock(f"{prefix}Dispatch_{len(ctxLkps)}")
                if before:
                    # I know it's not really a comment but this is the easiest way
                    # to get the lookup flag in there without reparsing it.
                    ctxLkps[before].statements.append(ast.Comment(f"{before};"))
            ctxLkp = ctxLkps[before]
            ctxLkp.statements.append(ast.Comment(f"# {after}"))

            # Insert mark glyph names after base glyph names if not specified otherwise.
            if "&" not in after:
                after = after.replace("*", "* &")

            baseGlyphNames = " ".join([s.name for s in statements])
            marks = ast.MarkClassName(self.context.markClasses[anchorKey]).asFea()

            # Replace * with base glyph names
            contextual = after.replace("*", f"[{baseGlyphNames}]")

            # Replace & with mark glyph names
            refLkpName = f"{prefix}_{len(refLkps)}"
            contextual = contextual.replace("&", f"{marks}' lookup {refLkpName}")
            ctxLkp.statements.append(ast.Comment(f"pos {contextual};"))

            # Then make the non-contextual lookup it references
            refLkp = ast.LookupBlock(refLkpName)
            refLkp.statements = [s.asAST() for s in statements]
            refLkps.append(refLkp)

    def _makeMkmkFeature(self, include):
        # First make the non-contextual lookups
        markLkps = []
        for anchorName, attachments in sorted(
            self.context.markToMarkAttachments.items()
        ):
            lkp = self._makeMarkToMarkLookup(anchorName, attachments, include)
            if lkp is not None:
                markLkps.append(lkp)

        # Then make the contextual ones
        refLkps = []
        ctxLkps = {}
        # We sort the full context by longest first. This isn't perfect
        # but it gives us the best chance that more specific contexts
        # (typically longer) will take precedence over more general ones.
        for context, glyph_anchor_pair in sorted(
            self.context.contextualMarkToMarkAnchors.items(), key=lambda x: -len(x[0])
        ):
            # Group by anchor
            attachments = defaultdict(list)
            for glyphName, anchor in glyph_anchor_pair:
                attachments[anchor.key].append(MarkToMarkPos(glyphName, [anchor]))
            self._makeContextualMarkLookup(
                attachments,
                context,
                refLkps,
                ctxLkps,
                prefix="ContextualMarkToMark",
            )

        ctxLkps = list(ctxLkps.values())
        if not markLkps and not ctxLkps:
            return None, []

        feature = ast.FeatureBlock("mkmk")
        if ctxLkps:
            lookups = markLkps + refLkps + ctxLkps
            for lookup in markLkps + ctxLkps:
                feature.statements.append(ast.LookupReferenceStatement(lookup))
        else:
            lookups = []
            for lookup in markLkps:
                feature.statements.append(lookup)

        return feature, lookups

    def _isAboveMark(self, anchor):
        if anchor.name in self.abvmAnchorNames:
            return True
        if (
            anchor.name in self.blwmAnchorNames
            or anchor.name.startswith("bottom")
            or anchor.name.startswith("nukta")
        ):
            return False
        # Glyphs uses (used to use?) a heuristic to guess whether an anchor
        # should go into abvm or blwm. (See
        # https://github.com/googlefonts/ufo2ft/issues/179#issuecomment-390391382)
        # However, this causes issues in variable fonts where an anchor in one
        # master is assigned to a different feature from the same anchor in
        # another master if the Y-coordinates happen to straddle the threshold
        # coordinate. For simplicity, we just place all unknown anchors into
        # the abvm feature.
        return True

    def _isBelowMark(self, anchor):
        return not self._isAboveMark(anchor)

    def _makeAbvmOrBlwmFeature(self, tag, include):
        if tag == "abvm":
            marksFilter = self._isAboveMark
        elif tag == "blwm":
            marksFilter = self._isBelowMark
        else:
            raise AssertionError(tag)

        baseLkps = []
        for attachments in self.context.groupedMarkToBaseAttachments:
            i = len(baseLkps)
            lookup = self._makeMarkLookup(
                f"{tag}_mark2base{'_' + str(i) if i > 0 else ''}",
                attachments,
                include=include,
                marksFilter=marksFilter,
            )
            if lookup:
                baseLkps.append(lookup)
        ligaLkps = []
        for attachments in self.context.groupedMarkToLigaAttachments:
            i = len(ligaLkps)
            lookup = self._makeMarkLookup(
                f"{tag}_mark2liga{'_' + str(i) if i > 0 else ''}",
                attachments,
                include=include,
                marksFilter=marksFilter,
            )
            if lookup:
                ligaLkps.append(lookup)
        mkmkLookups = []
        for anchorName, attachments in sorted(
            self.context.markToMarkAttachments.items()
        ):
            lkp = self._makeMarkToMarkLookup(
                anchorName,
                attachments,
                include=include,
                marksFilter=marksFilter,
                featureTag=tag,
            )
            if lkp is not None:
                mkmkLookups.append(lkp)

        if not any([baseLkps, ligaLkps, mkmkLookups]):
            return

        feature = ast.FeatureBlock(tag)
        for baseLkp in baseLkps:
            feature.statements.append(baseLkp)
        for ligaLkp in ligaLkps:
            feature.statements.append(ligaLkp)
        feature.statements.extend(mkmkLookups)
        return feature

    def _makeFeatures(self):
        ctx = self.context

        # First do non-contextual lookups
        ctx.groupedMarkToBaseAttachments = self._groupAttachments(
            self._makeMarkToBaseAttachments()
        )
        ctx.groupedMarkToLigaAttachments = self._groupAttachments(
            self._makeMarkToLigaAttachments()
        )
        ctx.markToMarkAttachments = self._makeMarkToMarkAttachments()

        baseClass = self.context.gdefClasses.base
        ligatureClass = self.context.gdefClasses.ligature
        markClass = self.context.gdefClasses.mark
        (
            ctx.contextualMarkToBaseAnchors,
            ctx.contextualMarkToLigaAnchors,
            ctx.contextualMarkToMarkAnchors,
        ) = self._makeContextualAttachments(baseClass, ligatureClass, markClass)

        abvmGlyphs, notAbvmGlyphs = self._getAbvmGlyphs()

        def isAbvm(glyphName):
            return glyphName in abvmGlyphs

        def isNotAbvm(glyphName):
            return glyphName in notAbvmGlyphs

        features = {}
        lookups = []
        todo = ctx.todo
        if "mark" in todo:
            mark, markLookups = self._makeMarkFeature(include=isNotAbvm)
            if mark is not None:
                features["mark"] = mark
                lookups.extend(markLookups)
        if "mkmk" in todo:
            mkmk, mkmkLookups = self._makeMkmkFeature(include=isNotAbvm)
            if mkmk is not None:
                features["mkmk"] = mkmk
                lookups.extend(mkmkLookups)
        if "abvm" in todo or "blwm" in todo:
            if abvmGlyphs:
                for tag in ("abvm", "blwm"):
                    if tag not in todo:
                        continue
                    feature = self._makeAbvmOrBlwmFeature(tag, include=isAbvm)
                    if feature is not None:
                        features[tag] = feature

        return features, lookups

    def _getAbvmGlyphs(self):
        glyphSet = set(self.getOrderedGlyphSet().keys())
        scriptsUsingAbvm = self.scriptsUsingAbvm
        if self.context.feaScripts:
            # https://github.com/googlefonts/ufo2ft/issues/579 Some characters
            # can be used in multiple scripts and some of these scripts might
            # need an abvm feature and some might not, so we filter-out the
            # abvm scripts that the font does not intend to support.
            scriptsUsingAbvm = scriptsUsingAbvm & self.context.feaScripts
        if scriptsUsingAbvm:
            cmap = self.makeUnicodeToGlyphNameMapping()
            unicodeIsAbvm = partial(unicodeInScripts, scripts=scriptsUsingAbvm)

            def unicodeIsNotAbvm(uv):
                return bool(unicodeScriptExtensions(uv) - self.scriptsUsingAbvm)

            if any(unicodeIsAbvm(uv) for uv in cmap):
                # If there are any characters from Indic/USE/Khmer scripts in
                # the cmap, we compile a temporary GSUB table to resolve
                # substitutions and get the set of all the relevant glyphs,
                # including alternate glyphs.
                gsub = self.compileGSUB()
                extras = self.extraSubstitutions()
                glyphGroups = classifyGlyphs(unicodeIsAbvm, cmap, gsub, extras)
                # the 'glyphGroups' dict is keyed by the return value of the
                # classifying include, so here 'True' means all the
                # Indic/USE/Khmer glyphs
                abvmGlyphs = glyphGroups.get(True, set())

                # If a character can be used in Indic/USE/Khmer scripts as well
                # as other scripts, we want to return it in both 'abvmGlyphs'
                # (done above) and 'notAbvmGlyphs' (done below) sets.
                glyphGroups = classifyGlyphs(unicodeIsNotAbvm, cmap, gsub, extras)
                notAbvmGlyphs = glyphGroups.get(True, set())
                # Since cmap might not cover all glyphs, we union with the
                # glyph set.
                notAbvmGlyphs |= glyphSet - abvmGlyphs
                return abvmGlyphs, notAbvmGlyphs
        return set(), glyphSet

    def _write(self):
        self._pruneUnusedAnchors()

        newClassDefs = self._makeMarkClassDefinitions()
        self._setBaseAnchorMarkClasses()

        features, lookups = self._makeFeatures()
        if not features:
            return False

        feaFile = self.context.feaFile

        self._insert(
            feaFile=feaFile,
            markClassDefs=newClassDefs,
            features=[features[tag] for tag in sorted(features.keys())],
            lookups=lookups,
        )

        return True
