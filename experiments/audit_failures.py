#!/usr/bin/env python3
"""Audit the questions no arm ever solved. No model calls, no reruns.

A question that every system fails is either a hard question or a broken one, and the two look
identical in a score table. This separates them mechanically:

  * `shape` - the gold is a list or object and the answer is prose, so the frozen grader scores
    zero however right the answer is. Reported as `names_all_gold` when the prose does name every
    gold symbol.
  * `gold` - the gold's own claim disagrees with an independent ripgrep enumeration of the corpus,
    so the question cannot be scored until the gold is repaired.
  * `unqualified` - the answer names the gold symbol but not its file, and the name is defined more
    than once, so no resolver can accept it.
  * `wrong` - the answer names something else entirely.

Only the last category is evidence about retrieval or reasoning. Everything else is evidence about
the benchmark.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

import quality_pass

NAME = re.compile(r"[A-Za-z_$][\w$]*")


def gold_symbols(answer):
    """Every (path, name) pair a typed gold answer asserts, whatever its shape."""
    found = []

    def walk(value):
        if isinstance(value, str) and "::" in value:
            path, _, rest = value.partition("::")
            found.append((path, rest.split("::")[-1]))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(answer)
    return found


def call_sites(corpus, name):
    """Independent enumeration of a helper's call sites, by ripgrep over the corpus."""
    found = subprocess.run(
        ["rg", "--no-config", "-n", "--no-heading", rf"\b{re.escape(name)}\s*\(",
         "-g", "*.ts", "-g", "*.tsx", "."],
        cwd=corpus, capture_output=True, text=True, timeout=120)
    sites = []
    for line in found.stdout.splitlines():
        path, _, rest = line.partition(":")
        path = path.lstrip("./")
        # A definition line is not a call site.
        if re.search(rf"(function|const|let|class)\s+{re.escape(name)}\b", rest):
            continue
        sites.append(path)
    return sites


SCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
C_SUFFIXES = (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")
BRACE_SUFFIXES = SCRIPT_SUFFIXES + C_SUFFIXES + (".rs", ".go", ".java")
RAW_STRING = re.compile(r'r(#*)"')
CHAR_LITERAL = re.compile(r"'(?:\\.|[^\\'])'")

# Words that can stand exactly where a declaration's name would in a script, so a block they open
# is anonymous rather than a definition. Without this, `if (x) {` would attribute every call
# inside it to `if`, and `new (resolveClass(reader))(` to `new`. Rust never consults this list:
# its only declaration keyword is `fn`, so a match arm or an `if let Ok(x) = f() {` guard cannot
# reach a name at all.
CONTROL_WORDS = frozenset({
    "if", "for", "while", "switch", "catch", "do", "else", "try", "finally", "return", "new",
    "typeof", "await", "yield", "throw", "case", "with", "in", "of", "instanceof", "delete",
    "void", "super", "function", "class", "interface", "enum", "namespace", "module",
    "import", "export", "declare", "extends", "implements",
    # Go, Java and C write these in the same position a declaration writes its name.
    "go", "defer", "select", "range", "chan", "map", "struct", "union", "package", "sizeof",
    "static_assert", "assert", "synchronized", "throws", "this", "constexpr", "decltype",
    "operator", "template", "using", "public", "private", "protected", "static", "final",
    "elif", "goto",
})

# Keywords that can stand immediately in front of a call and make the line a statement rather
# than a declaration: `return normalize(row);` is not a method named `normalize`, though it is
# spelled exactly like one once a return type is allowed in front of the name. Modifiers such as
# `public` are deliberately absent - they really do precede a declaration's name.
STATEMENT_KEYWORDS = frozenset({
    "return", "throw", "new", "delete", "typeof", "await", "yield", "case", "in", "of",
    "instanceof", "sizeof", "else", "do", "goto", "co_return", "co_await", "assert",
})

# Declaration syntax is read per language, because the same line means different things in each.
# Rust declares a callable with `fn` and nothing else - which is also the only Rust form the
# definition index knows - so `let render = |x| {`, a match arm such as `Ok(file) => {`, an
# `impl` block and a builder call all open anonymous frames there, while in TypeScript a bare
# `name(args) {` really is a method. Applying the script rules to Rust credited match arms and
# closures with names like `Ok` and `custom_str_cmp`, which is a caller that does not exist.
RUST_DECLARATIONS = (
    re.compile(r"\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(?:async\s+)?"
               r"(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_]\w*)"),
)

# The member pattern is last so that control flow reaches it and is rejected by name rather than
# by accident.
SCRIPT_MEMBER = re.compile(
    r"\s*(?:(?:public|private|protected|static|readonly|abstract|override|async|"
    r"get|set|declare)\s+)*\*?\s*([A-Za-z_$][\w$]*)\s*\??\s*[(<]")
SCRIPT_BINDING = re.compile(r"\s*(?:export\s+)?(?:declare\s+)?(?:const|let|var)\s+"
                            r"([A-Za-z_$][\w$]*)\s*[:=]")
SCRIPT_DECLARATIONS = (
    re.compile(r"\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
               r"function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:const\s+)?(?:abstract\s+)?"
               r"(?:class|interface|enum|namespace|module|type)\s+([A-Za-z_$][\w$]*)"),
    SCRIPT_BINDING,
    SCRIPT_MEMBER,
)
# A block opened by an arrow belongs to the callback, not to the line that passed it, unless the
# arrow is the binding's own value: `const f = (a) => {` declares `f`, while
# `const o = new ResizeObserver(() => {` and `this.onEvent(() => {` declare nothing.
CALLBACK_BLOCK = re.compile(r"=>\s*\{")
ARROW_VALUE = re.compile(r"=\s*(?:async\s+)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]*?)?=>")
# A binding names a frame only when it binds a callable, exactly as the symbol index requires:
# `const applied = astUtils.skipChainExpression(node.callee)` is a value, and naming its frame
# credits a call to a local const that no index defines - the shape of the caller defect this
# project already paid for once, that time in the server rather than in the oracle.
BINDING_VALUE = re.compile(r"=\s*(?:async\s+)?(?:function\b|class\b)")
# A shorthand method in an object literal - `context.report({ fix(fixer) { ... } })`, which is how
# every ESLint rule writes a fixer - opens its body inside an argument list, so the
# parenthesis-depth rule that skips callbacks would skip it too. The systems under test index it
# as a definition (`method_definition`) and attribute calls inside it to it, so the oracle must.
SCRIPT_SHORTHAND = re.compile(r"\s*(?:async\s+)?\*?\s*([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*\{")

# Go names a frame only for `func` - with or without a receiver - and for `type`. A composite
# literal such as `return &Table{Rows: rows}` opens a brace directly after a type name, so
# reading names from anything else would credit every struct literal with the calls beneath it.
GO_DECLARATIONS = (
    re.compile(r"\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"\s*type\s+([A-Za-z_]\w*)\s"),
)

# Java always writes modifiers or a return type before the name, and a constructor writes
# neither a return type nor a keyword. `if (`, `for (` and `catch (` stand in the same position,
# so they are refused by name through CONTROL_WORDS.
JAVA_DECLARATIONS = (
    re.compile(r"\s*(?:@\w+(?:\([^)]*\))?\s+)*"
               r"(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|"
               r"strictfp)\s+)*(?:class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\s*(?:@\w+(?:\([^)]*\))?\s+)*"
               r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|"
               r"default|strictfp)\s+)*(?:<[^>]*>\s*)?(?:[\w$.<>\[\],?]+(?:\.\.\.)?\s+)?"
               r"([A-Za-z_$][\w$]*)\s*\("),
)

# C and C++ name a function through its declarator: the identifier that opens the parameter
# list, whose member half is the name when it is written `Engine::render`, and which may be a
# destructor. A struct, class or namespace header names its own frame. A line continuing a
# constructor's member-initialiser list (`: direction_(kForward),`) declares nothing - reading
# it as a declaration named the frame after the member instead of the constructor.
C_DECLARATIONS = (
    re.compile(r"\s*(?:typedef\s+)?(?:struct|union|enum|class|namespace)\s+"
               r"(?:[A-Z][A-Z0-9_]*\s+)*([A-Za-z_]\w*)"),
    re.compile(r"\s*(?![:,])[^=;]*?(?:\b[A-Za-z_]\w*::)?(~?\b[A-Za-z_]\w*)\s*\("),
)

DECLARATIONS = {
    ".rs": RUST_DECLARATIONS,
    ".go": GO_DECLARATIONS,
    ".java": JAVA_DECLARATIONS,
    **{suffix: C_DECLARATIONS for suffix in C_SUFFIXES},
    **{suffix: SCRIPT_DECLARATIONS for suffix in SCRIPT_SUFFIXES},
}

# A frame opened by a type header holds members; a frame opened by a callable header holds
# statements. Only C consults the difference, to refuse a macro block inside a function body.
TYPE_RULES = frozenset({
    GO_DECLARATIONS[1], JAVA_DECLARATIONS[0], C_DECLARATIONS[0], SCRIPT_DECLARATIONS[1],
})


def without_literals(source, suffix=".ts"):
    """Each line with its comments and string, char and raw-string literals blanked out.

    Brace counting is only meaningful over code. A `{` inside a JSDoc block, a string or a Rust
    raw string opens a scope that never closes, which would mis-attribute every call after it in
    the file. Only Rust gets the lifetime exception: there `'a` is not a char literal and reading
    it as one swallows the real braces that follow on the same line, while in a script `'...'` is
    an ordinary string and must be blanked whole.
    """
    cleaned, in_block = [], False
    for text in source:
        out, index, length = [], 0, len(text)
        while index < length:
            if in_block:
                if text.startswith("*/", index):
                    in_block, index = False, index + 2
                else:
                    index += 1
                continue
            if text.startswith("/*", index):
                in_block, index = True, index + 2
                continue
            if text.startswith("//", index):
                break
            raw = RAW_STRING.match(text, index) if suffix == ".rs" else None
            if raw:
                closing = '"' + raw.group(1)
                position = text.find(closing, raw.end())
                index = length if position < 0 else position + len(closing)
                out.append(" ")
                continue
            character = text[index]
            if character == "'" and suffix == ".rs" and not CHAR_LITERAL.match(text, index):
                out.append(" ")
                index += 1
                continue
            if character in "\"'`":
                index += 1
                while index < length:
                    if text[index] == "\\":
                        index += 2
                        continue
                    if text[index] == character:
                        index += 1
                        break
                    index += 1
                out.append(" ")
                continue
            out.append(character)
            index += 1
        cleaned.append("".join(out))
    return cleaned


# The rules whose match is a callable rather than a type, per language. C uses this to refuse a
# declaration written inside a function body, which can only be a macro.
def declaration(text, suffix=".ts"):
    """(name, is_callable) for a brace-language line, or (None, False) when it declares nothing."""
    rules = DECLARATIONS.get(suffix)
    if rules is None:
        return None, False
    for expression in rules:
        match = expression.match(text)
        if not match:
            continue
        name = match.group(1)
        # Rust reaches a name only through `fn`, so a control keyword cannot stand where its
        # name would; every other language here writes `if (`, `for (` and `catch (` in exactly
        # the position a declaration writes its name, and must refuse them by name. The keyword
        # can also stand immediately *before* the name - `return normalize(row);` reads as a
        # method declaration named `normalize` to any pattern that allows a return type - so the
        # word in front of the name is refused the same way.
        # Go and Rust both reach a name only through a keyword of their own - `func`, `fn` - so a
        # control word cannot stand where the name stands, and refusing it by name only loses real
        # definitions: `func (tw *storeTxnWrite) delete(key []byte)` is a method whose name is a
        # JavaScript operator, and dropping it dropped every call site inside its body from the
        # oracle's caller sets. Every other family here writes `if (`, `for (` and `catch (` in the
        # position a declaration writes its name, and must still refuse them.
        if rules not in (RUST_DECLARATIONS, GO_DECLARATIONS):
            if name in CONTROL_WORDS:
                return None, False
            before = re.findall(r"[A-Za-z_$][\w$]*", text[:match.start(1)])
            if before and before[-1] in STATEMENT_KEYWORDS:
                return None, False
        if CALLBACK_BLOCK.search(text, match.end()) and (
                expression is SCRIPT_MEMBER or
                (expression is SCRIPT_BINDING and not ARROW_VALUE.search(text, match.end()))):
            return None, False
        if expression is SCRIPT_BINDING and not (
                ARROW_VALUE.search(text, match.end()) or BINDING_VALUE.search(text, match.end())):
            return None, False
        return name, expression not in TYPE_RULES
    return None, False


def declared_name(text, suffix=".ts"):
    """The name a brace-language line introduces, or None when it introduces nothing."""
    return declaration(text, suffix)[0]


def enclosing(path, line, detail=False):
    """The definition a line sits inside, by reading the file. Independent of every index.

    Python is indentation-scoped, so the enclosing definition is not merely the nearest `def` or
    `class` indented less than the call site: a nested helper defined earlier in the same body is
    also indented less, and it has already closed. The bound therefore tightens on every statement
    shallower than the current one, so only a definition that still contains the line can match.

    Brace languages are scoped by `{}`, so they are matched on their declaration syntax instead:
    the file is scanned forward over a copy with comments and literals blanked, every `{` pushes
    the name its header declared and every `}` pops, and the answer is the innermost frame that
    carries a name. Declaration syntax is read per language - Rust names a frame only for `fn`,
    scripts also for `function`, a type, a binding and a class member - so a Rust match arm and a
    JavaScript callback both open anonymous frames. Anonymous blocks push nothing, so a call
    inside a closure is attributed to the definition containing the closure, exactly as the
    systems under test attribute it. A declaration whose body brace lands on a later line, which
    a wrapped TypeScript signature or a Rust `where` clause both produce, stays pending until
    that brace opens.

    Returns the leaf name only, or None for a suffix this cannot parse. With `detail`, returns
    `(name, inside_a_body)`: a line that merely continues a declaration whose body has not opened
    - the second line of a wrapped C++ prototype, or the annotation after it - is not inside any
    body, and a `name(...)` written there is an attribute rather than a call.
    """
    source = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if path.suffix == ".py":
        result = None
        call = source[line - 1] if line <= len(source) else ""
        limit = len(call) - len(call.lstrip())
        for position in range(min(line, len(source)) - 1, -1, -1):
            text = source[position]
            stripped = text.strip()
            # Decorators and the tail of a wrapped signature or call sit at the definition's own
            # indentation without being statements in its parent block, so they must not tighten
            # the bound - doing so skips past the `def` and credits the enclosing class instead.
            if not stripped or stripped[0] in "#@)]},":
                continue
            indent = len(text) - len(text.lstrip())
            if indent >= limit:
                continue
            header = re.match(r"(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", text)
            if header:
                return (header.group(2), True, True) if detail else header.group(2)
            # A shallower statement means the inner block has closed; nothing at or below this
            # indentation can enclose the call any more.
            limit = indent
        return (result, True, True) if detail else result
    if path.suffix not in BRACE_SUFFIXES:
        return (None, False, False) if detail else None
    # Each frame is None, or (name, opened_by_a_callable_header).
    stack, pending, owner, depth = [], None, None, 0
    inline_owner = None
    for text in without_literals(source, path.suffix)[:line]:
        inline_owner = None
        # Whether the line stands inside a body is judged as the line begins, exactly as its
        # owner is: a declaration that is still waiting for its brace holds the line, even though
        # the line's own `;` ends the wait.
        continuing = pending is not None
        declared, callable_header = declaration(text, path.suffix)
        # C and C++ have no nested functions, so a brace opened inside a function body belongs to
        # a macro, not to a declaration. `TEST("...") { ... }` in a Redis test reads exactly like
        # a function header, and naming its frame credited every call inside the macro to `TEST`
        # instead of to the function that really makes them.
        if declared is not None and path.suffix in C_SUFFIXES and any(
                entry is not None and entry[1] for entry in stack):
            declared, callable_header = None, False
        # The owner of the line is the stack as it stands before the line's own braces open, so a
        # declaration belongs to its parent while its body belongs to it - unless a header is
        # already waiting for its body, in which case the lines between the two are part of that
        # declaration. A C++ constructor's initialiser list and a default argument both call
        # from inside the definition, and the systems under test attribute them to it.
        frame = pending if pending is not None else next(
            (entry for entry in reversed(stack) if entry), None)
        owner, owner_callable = frame if frame is not None else (None, False)
        opened, born, statement = [], [], depth == 0
        for character in text:
            if character == "{":
                opened.append((len(stack), depth))
                born.append((len(stack), depth))
                stack.append(None)
            elif character == "}" and stack:
                if opened and opened[-1][0] == len(stack) - 1:
                    opened.pop()
                stack.pop()
            elif character == "(":
                depth += 1
            elif character == ")":
                depth = max(depth - 1, 0)
        stripped = text.strip()
        # A header already waiting for its body wins over anything the continuation lines look
        # like: a C++ constructor's member-initialiser list (`current_(nullptr),
        # direction_(kForward) {`) reads exactly like a declaration, and letting it win named
        # the frame after a member instead of after the constructor.
        label = pending if pending is not None else (
            (declared, callable_header) if declared is not None else None)
        if born:
            # A declaration's body brace stands outside every argument list, so only a `{` at
            # parenthesis depth zero can carry the name. That skips the object literal in
            # `emit(this._store, value => {` while still reaching the body in
            # `(): { a: b } {`, whose type-literal frame closed again on the same line.
            body = next((position for position, level in opened if level == 0), None)
            if body is None and path.suffix in SCRIPT_SUFFIXES and declared is not None \
                    and SCRIPT_SHORTHAND.match(text):
                # ...except a shorthand method, whose body really does open inside the argument
                # list of the call it is being passed to.
                body = opened[0][0] if opened else None
            if label is not None and body is not None:
                stack[body] = label
            # A definition written on one line - `const char* end() const { return data(); }`, a
            # Go one-line method, a C++ constructor whose initialiser list ends `cache_(
            # NewLRUCache(entries)) {}` - opens and closes its own frame before the line ends, so
            # nothing is left on the stack to name it. The call on that line is still made by it:
            # Redis defines three one-line wrappers around dictGenCaseHashFunction and LevelDB
            # builds its table cache inside an initialiser, and reading the stack alone credited
            # those calls to the file's namespace.
            if label is not None and any(level == 0 for _, level in born):
                inline_owner = label[0]
            pending = None
        elif declared is not None and statement and not stripped.endswith(";") and (
                not stripped.endswith(",") or depth > 0):
            # A wrapped signature puts the body brace on a later line, so the name waits for it:
            # a C parameter list broken after a comma, a TypeScript signature broken between
            # parameters, a Rust `where` clause. Only a header that starts its own statement may
            # wait - an argument such as `getActiveWindow(),` on the next line of a call is not a
            # declaration, and letting it wait names the callback that follows it.
            pending = (declared, callable_header)
        elif stripped.endswith((";", "}")) or not stripped:
            pending = None
        if stripped.endswith((";", "}")) or not stripped:
            # A statement cannot continue past its terminator, so depth resyncs here. Without
            # this, one unbalanced parenthesis inside a regex literal leaves every later body
            # brace looking like an argument and silently unnames the rest of the file.
            depth = 0
    answer = inline_owner or owner
    if not detail:
        return answer
    # `inline_owner` is only ever set from a definition's own header, so it is callable by
    # construction; otherwise the frame that holds the line decides.
    return (answer, inline_owner is not None or not continuing,
            True if inline_owner is not None else owner_callable)


def annotation(statement, declared, name):
    """Is `name(...)` an attribute written after a declarator, rather than a call?

    LevelDB annotates its declarations for the thread-safety analyser:
    `Status DoCompactionWork(CompactionState* compact) EXCLUSIVE_LOCKS_REQUIRED(mutex_);` and
    `void Unlock() UNLOCK_FUNCTION() { mu_.unlock(); }`. The macro stands outside the parameter
    list of the declaration, and no system under test reports it as a call site, so counting it
    demands callers that cannot be found. A default argument - `Status NotSupported(const Slice&
    msg, const Slice& msg2 = Slice())` - is inside the parameter list and is a real call, which
    is why position decides rather than shape.
    """
    if not declared or declared == name or not statement.endswith((";", "{", "}")):
        return False
    boundary = "" if declared.startswith("~") else r"\b"
    header = re.search(rf"{boundary}{re.escape(declared)}\s*\(", statement)
    target = re.search(rf"\b{re.escape(name)}\s*\(", statement)
    if not header or not target or target.start() < header.end():
        return False
    span = statement[header.end() - 1:target.start()]
    # A body brace between the declarator and the name means the call is inside the definition,
    # not attached to its declaration: `int Engine::render(int row) const { return normalize(row);
    # }` calls, `void Unlock() UNLOCK_FUNCTION() { ... }` annotates.
    if "{" in span:
        return False
    depth = 0
    for character in span:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
    return depth == 0


def in_literal(line, name):
    """Whether every `name(` on this line sits inside a string literal.

    Quotes are counted before the match rather than parsed: a line that opens a string and does
    not close it before the call is a format string, and one that has closed it is code. Escaped
    quotes are rare inside a call and cost a false negative, never a false positive, because a
    line with any call outside a literal is kept.
    """
    for match in re.finditer(rf"\b{re.escape(name)}\s*\(", line):
        before = line[:match.start()]
        quoted = before.count('"') - before.count('\\"')
        if quoted % 2 == 0 and before.count("`") % 2 == 0 and before.count("'") % 2 == 0:
            return False
    return True


def true_callers(corpus, name, defining_path, include_defining_file=False):
    """Every enclosing definition that calls `name`, from source alone.

    Calls written inside the helper's own defining file are excluded by default: that is the
    convention every caller gold in this repository was authored under and that `language_audit`
    compares against. A suite that wants the question a user actually asks - "who calls this" -
    passes `include_defining_file=True` and states no exclusion in its prose.
    """
    globs = []
    for suffix in (".py", *BRACE_SUFFIXES):
        globs += ["-g", f"*{suffix}"]
    # A `.d.ts` file holds signatures and no bodies, so `isPathIgnored(filePath: string):
    # Promise<boolean>;` is a declaration that reads exactly like a call and no system under test
    # reports it as one.
    globs += ["-g", "!*.d.ts"]
    found = subprocess.run(
        ["rg", "--no-config", "-n", "--no-heading", rf"\b{re.escape(name)}\s*\(", *globs, "."],
        cwd=corpus, capture_output=True, text=True, timeout=120)
    callers = set()
    for line in found.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, body = rest.partition(":")
        path = path.lstrip("./")
        if (path == defining_path and not include_defining_file) or not number.isdigit():
            continue
        # Nor is a format string. `s.printf("RegisterService(%q)", ...)` names the helper inside a
        # string literal, and counting it demands that an exhaustive gold name a caller that does
        # not call anything. It was invisible while the defining file was always excluded.
        if in_literal(body, name):
            continue
        # A declaration is not a call site. Python, Rust and the scripts say so with a keyword;
        # C, C++ and Java write a definition header or a member prototype in the same shape as a
        # call, so those are read with the same declaration rules the attribution uses. What
        # separates a prototype from a call is the return type in front of it:
        # `void WriteBatchInternal::SetSequence(WriteBatch*, SequenceNumber);` declares, while
        # `WriteBatchInternal::SetSequence(batch, seq);` calls, and treating the second as a
        # declaration lost real callers in every C++ file that qualifies a static call.
        if re.search(rf"(function|const|let|class|def|fn|func|type)\s+{re.escape(name)}\b", body):
            continue
        statement = body.strip()
        qualified = re.search(rf"(?:\b[A-Za-z_]\w*\s*(?:::|\.|->)\s*)*\b{re.escape(name)}\s*\(",
                              statement)
        preceded = bool(statement[:qualified.start()].strip()) if qualified else True
        if declared_name(body, Path(path).suffix) == name and (
                statement.endswith("{") or (statement.endswith(";") and preceded)):
            continue
        if annotation(statement, declared_name(body, Path(path).suffix), name):
            continue
        # Nor is prose. `Field.set_cached_value()` written inside a comment is documentation, and
        # counting it demands that an exhaustive gold name a caller that does not exist.
        match = re.search(rf"\b{re.escape(name)}\s*\(", body)
        if match and re.search(r"#|//", body[:match.start()]):
            continue
        owner, inside_body, callable_owner = enclosing(Path(corpus) / path, int(number),
                                                       detail=True)
        # A `name(...)` on a line that only continues a declaration - LevelDB wraps a prototype
        # and puts `EXCLUSIVE_LOCKS_REQUIRED(mutex_);` on the next line - annotates that
        # declaration instead of calling anything, and no system under test reports it.
        if not owner or (Path(path).suffix in C_SUFFIXES and not inside_body):
            continue
        # Nothing calls anything from inside a type: `MetricsRecorder() stats.MetricsRecorder`
        # written in a Go `interface` body declares a method, and counting it as a call demands
        # that an exhaustive gold name the interface as a caller of its own member.
        if Path(path).suffix == ".go" and not callable_owner:
            continue
        callers.add(f"{path}::{owner}")
    return sorted(callers)


def audit(run, questions, corpus, helpers):
    tasks = {task["id"]: task for task in json.loads(questions.read_text(encoding="utf-8"))}
    index = quality_pass.definitions(corpus)
    trials = []
    for path in sorted(run.glob("trial-*/run.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["status"] == "provider_error":
            continue
        trials.append(state)
    by_task = {}
    for state in trials:
        by_task.setdefault(state["task_id"], []).append(state)

    findings = {}
    for task_id, states in sorted(by_task.items()):
        if any(state.get("resolved_correct") for state in states):
            continue
        task = tasks[task_id]
        gold = task["expected_json"]["answer"]
        pairs = gold_symbols(gold)
        rows = []
        for state in states:
            written = quality_pass.answer_json(state.get("answer"))
            text = written if isinstance(written, str) else json.dumps(written)
            named = {token for token in NAME.findall(text or "")}
            missing = sorted({name for _, name in pairs} - named)
            shape = type(gold).__name__ if not isinstance(written, type(gold)) else None
            ambiguous = [name for _, name in pairs
                         if name in named and len(index.get(name, ())) > 1
                         and not any(path in (text or "") for path, other in pairs if other == name)]
            rows.append({
                "system": state["system"],
                "repetition": state["repetition"],
                "answer": text or "",
                "names_all_gold": not missing,
                "missing_gold_names": missing,
                "shape_mismatch": shape,
                "unqualified_namesakes": ambiguous,
                "verdict": ("wrong" if missing else
                            "unqualified" if ambiguous else
                            "shape" if shape else "other"),
            })
        # A caller question's gold is checked against the corpus rather than taken on trust, but
        # only where the helper being called is unambiguous: an object gold names it, or the
        # operator names it with --helper. Guessing the helper from a list gold reads a common
        # name like `initialize` as the target and produces nonsense.
        gold_claims = ["::".join(pair) for pair in pairs]
        helper = helpers.get(task_id) or (
            gold.get("implementation") if isinstance(gold, dict) else None)
        verified, gold_defect = None, None
        if helper and "::" in helper:
            helper_path, helper_name = helper.split("::", 1)
            verified = true_callers(corpus, helper_name.split("::")[-1], helper_path)
            claimed = {claim for claim in gold_claims if claim != helper}
            if verified and claimed != set(verified):
                gold_defect = {
                    "helper": helper,
                    "claimed_callers": sorted(claimed),
                    "verified_callers": verified,
                    "claimed_but_not_found": sorted(claimed - set(verified)),
                    "found_but_not_claimed": sorted(set(verified) - claimed),
                }
        findings[task_id] = {
            "category": task["category"],
            "question": task["question"],
            "gold": gold,
            "gold_shape": type(gold).__name__,
            "gold_symbols": gold_claims,
            "gold_defect": gold_defect,
            "trials": rows,
            "verdicts": dict(Counter(row["verdict"] for row in rows)),
        }
    return {
        "version": "audit-failures-v1",
        "run": str(run),
        "questions_total": len(by_task),
        "questions_never_solved": len(findings),
        "trials_audited": sum(len(f["trials"]) for f in findings.values()),
        "verdicts": dict(Counter(row["verdict"] for f in findings.values() for row in f["trials"])),
        "findings": findings,
        "limitations": "Mechanical checks only: name presence, answer shape, namesake ambiguity, "
                       "and an independent call-site count. Whether a question is genuinely "
                       "ambiguous still requires reading the corpus.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", action="append", default=[],
                        help="task_id=path::symbol; the helper a caller question is about, when "
                             "the gold does not name it")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    helpers = dict(entry.split("=", 1) for entry in args.helper)
    result = audit(args.run.resolve(strict=True), args.questions, args.corpus.resolve(strict=True),
                   helpers)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("questions_total", "questions_never_solved", "trials_audited", "verdicts")},
                     indent=2))
    for task_id, finding in result["findings"].items():
        print(f"{task_id:38s} {finding['gold_shape']:5s} {finding['verdicts']}")
        if finding["gold_defect"]:
            defect = finding["gold_defect"]
            print(f"    gold defect: {len(defect['claimed_but_not_found'])} claimed callers absent, "
                  f"{len(defect['found_but_not_claimed'])} real callers unclaimed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
