"""
preprocessor.py — Deterministic code analysis before any LLM sees the code.

Zero tokens. Zero API calls. Pure Python ast.

Pipeline:
  1. safe_parse()              — parse code into AST
  2. extract_structure()       — pull all structural facts
  3. build_call_graph()        — NEW: maps which function calls which (1-hop expansion)
  4. quick_flags()             — deterministically pre-select agents
  5. extract_chunk_for_agent() — send each agent only relevant lines + called functions
  6. build_orchestrator_summary() — compact text for orchestrator instead of raw code
"""

import ast
from collections import Counter, defaultdict
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# KNOWN PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

DANGEROUS_IMPORTS  = {"os", "subprocess", "pickle", "eval", "exec", "shlex", "pty", "ctypes"}
DB_IMPORTS         = {"sqlite3", "psycopg2", "sqlalchemy", "pymysql", "cx_Oracle", "pymongo"}
STDLIB             = {"os", "sys", "re", "json", "math", "time", "datetime", "pathlib",
                      "typing", "collections", "itertools", "functools", "io", "abc",
                      "threading", "logging", "tempfile", "urllib", "http", "email"}
DANGEROUS_CALLS    = {"eval", "exec", "compile", "pickle.loads", "os.system",
                      "subprocess.call", "subprocess.run", "input"}
SQL_KEYWORDS       = {"SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE"}


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Safe parse
# ─────────────────────────────────────────────────────────────────────────────

def safe_parse(code: str) -> Optional[ast.AST]:
    try:
        return ast.parse(code) #gives full structural tree
    except SyntaxError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Extract structural facts
# ─────────────────────────────────────────────────────────────────────────────

def extract_structure(code: str, tree: ast.AST) -> dict:
    lines = code.splitlines()

    # Functions with metadata
    functions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            functions.append({
                "name":                  node.name,
                "line_start":            node.lineno,
                "line_end":              getattr(node, "end_lineno", node.lineno + 10),
                "args":                  [a.arg for a in node.args.args],
                "has_docstring":         _has_docstring(node),
                "has_return_annotation": node.returns is not None,
            })

    # Classes
    classes = [
        {"name": n.name, "line": n.lineno}
        for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
    ]

    # Imports
    imports = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for alias in n.names:
                imports.append(alias.name)
        elif isinstance(n, ast.ImportFrom):
            imports.append(n.module or "")

    # Duplicates
    func_names = [f["name"] for f in functions]
    duplicates = [name for name, count in Counter(func_names).items() if count > 1]

    # Loops
    loop_nodes   = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]
    nested_loops = _find_nested_loops(tree)

    # Complexity
    branches = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                          ast.With, ast.Assert, ast.comprehension))
    )

    # Dangerous calls
    dangerous_calls = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if name and any(d in name for d in DANGEROUS_CALLS):
                dangerous_calls.append({"call": name, "line": getattr(n, "lineno", "?")})

    # SQL strings
    sql_strings = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.s, str):
            if any(kw in n.s.upper() for kw in SQL_KEYWORDS):
                sql_strings.append({"line": n.lineno, "snippet": n.s[:60]})

    # f-strings
    fstring_lines = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) and hasattr(n, "lineno"):
            fstring_lines.append(n.lineno)

    return {
        "line_count":                  len(lines),
        "functions":                   functions,
        "classes":                     classes,
        "imports":                     imports,
        "duplicate_functions":         duplicates,
        "has_loops":                   bool(loop_nodes),
        "has_nested_loops":            bool(nested_loops),
        "nested_loop_lines":           nested_loops,
        "has_try_except":              any(isinstance(n, ast.Try) for n in ast.walk(tree)),
        "complexity_score":            branches,
        "dangerous_calls":             dangerous_calls,
        "sql_strings":                 sql_strings,
        "fstring_lines":               fstring_lines,
        "functions_without_docstring": [f["name"] for f in functions if not f["has_docstring"]],
        "functions_without_types":     [f["name"] for f in functions if not f["has_return_annotation"]],
    } #Replaces thousands of tokens with ~20 structured signals and enables deterministic routing


def _has_docstring(node: ast.FunctionDef) -> bool: #Checks if first statement in function is a string literal
    return bool(
        node.body and
        isinstance(node.body[0], ast.Expr) and
        isinstance(node.body[0].value, ast.Constant)
    )


def _call_name(node: ast.Call) -> Optional[str]: #eval(x) → "eval"
    if isinstance(node.func, ast.Name):
        return node.func.id
    elif isinstance(node.func, ast.Attribute):
        return f"{getattr(node.func.value, 'id', '?')}.{node.func.attr}"
    return None


def _find_nested_loops(tree: ast.AST) -> list:
    nested = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            for child in ast.walk(node):
                if child is not node and isinstance(child, (ast.For, ast.While)):
                    nested.append(node.lineno)
                    break
    return nested


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Call graph (the new piece)
#
# This solves the edge case from the document:
#
#   def run():
#       query = build_query(user_input)   # build_query is the unsafe one
#
# Without a call graph, we'd only send run() to the security agent
# and miss that build_query() is the actual problem.
#
# With the call graph, we know run() → build_query(), so we expand
# the chunk to include build_query() too — 1 hop.
# ─────────────────────────────────────────────────────────────────────────────

def build_call_graph(tree: ast.AST) -> dict:
    """
    Build a map of:
        function_name → set of function names it calls

    Example:
        {
          "run":         {"build_query", "connect_db"},
          "build_query": {"os.system"},
          "connect_db":  {"sqlite3.connect"},
        }

    This is used to expand chunks 1 hop:
    if run() calls build_query() and build_query() is dangerous,
    include build_query() in the chunk sent to the security agent.
    """

    # First pass: collect line ranges for each function
    func_ranges = {}   # name → (start_line, end_line)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            func_ranges[node.name] = (
                node.lineno,
                getattr(node, "end_lineno", node.lineno + 50)
            )

    # Second pass: for each function, find all calls made inside it
    call_graph = defaultdict(set)  # caller → {callees}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            caller = node.name
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    callee = _call_name(child)
                    if callee:
                        call_graph[caller].add(callee)

    return {
        "graph":       dict(call_graph),   # caller → set of callees
        "func_ranges": func_ranges,        # name → (start, end) line numbers
    }


def expand_with_dependencies(
    func_names: list,
    call_graph: dict,
    func_ranges: dict,
    lines: list,
    hops: int = 1
) -> set:
    """
    Given a list of function names that are flagged as relevant,
    expand to include functions they call (up to `hops` levels deep).

    Example with hops=1:
        run() is flagged → also include build_query() because run() calls it
        build_query() calls os.system → include os.system call lines too

    Returns a set of line indices to include in the chunk.
    """
    relevant_lines = set()
    visited        = set()
    to_expand      = set(func_names)

    for _ in range(hops):
        next_expand = set()
        for fname in to_expand:
            if fname in visited:
                continue
            visited.add(fname)

            # Add this function's lines
            if fname in func_ranges:
                start, end = func_ranges[fname]
                relevant_lines.update(range(start - 1, min(end, len(lines))))

            # Queue its callees for next hop
            callees = call_graph.get("graph", {}).get(fname, set())
            for callee in callees:
                # strip method prefix e.g. "conn.execute" → "execute"
                short = callee.split(".")[-1]
                if short in func_ranges and short not in visited:
                    next_expand.add(short)

        to_expand = next_expand

    return relevant_lines


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Quick flags (deterministic agent pre-selection)
# ─────────────────────────────────────────────────────────────────────────────

def quick_flags(imports: list, structure: dict) -> dict:
    import_set = set(imports)

    flags = {
        "needs_analyzer":    True,
        "needs_docs":        True,
        "needs_security":    bool(
            import_set & DANGEROUS_IMPORTS or
            structure["dangerous_calls"] or
            structure["sql_strings"] or
            structure["fstring_lines"]
        ),
        "needs_performance": bool(
            structure["has_nested_loops"] or
            structure["complexity_score"] > 10
        ),
        "needs_dependency":  bool(import_set - STDLIB),
        "needs_style":       structure["line_count"] > 20,
    }

    flags["pre_selected"] = [
        agent.replace("needs_", "")
        for agent, needed in flags.items()
        if agent.startswith("needs_") and needed
    ]

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Per-agent focused chunks (with dependency expansion)
# ─────────────────────────────────────────────────────────────────────────────

def extract_chunk_for_agent(
    code: str,
    tree: ast.AST,
    agent: str,
    call_graph: dict
) -> str:
    """
    Extract the minimal relevant code for each agent.
    Now uses call graph to expand 1 hop — so if run() calls build_query()
    and build_query() is dangerous, both are included in the security chunk.
    """
    lines = code.splitlines()

    try:
        if agent == "security":
            return _chunk_security(tree, lines, call_graph)
        elif agent == "performance":
            return _chunk_performance(tree, lines, call_graph)
        elif agent == "docs":
            return _chunk_docs(tree, lines)
        elif agent == "dependency":
            return _chunk_imports(tree, lines)
        elif agent == "style":
            return code  # style needs full code
        else:
            return code  # analyzer gets full code
    except Exception:
        return code


def _chunk_security(tree: ast.AST, lines: list, call_graph: dict) -> str:
    """
    Send security agent only:
    - functions with dangerous calls / SQL / f-strings
    - PLUS their 1-hop callees (dependency expansion)
    - import lines always included
    """
    relevant      = set()
    flagged_funcs = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # Check if this function contains anything dangerous
            is_dangerous = False

            for child in ast.walk(node):
                # Dangerous call
                if isinstance(child, ast.Call):
                    name = _call_name(child)
                    if name and any(d in name for d in DANGEROUS_CALLS):
                        is_dangerous = True

                # SQL string
                if isinstance(child, ast.Constant) and isinstance(child.s, str):
                    if any(kw in child.s.upper() for kw in SQL_KEYWORDS):
                        is_dangerous = True

                # f-string
                if isinstance(child, ast.JoinedStr):
                    is_dangerous = True

            if is_dangerous:
                flagged_funcs.append(node.name)

    # Expand 1 hop: also include functions called by dangerous functions
    expanded_lines = expand_with_dependencies(
        flagged_funcs,
        call_graph,
        call_graph.get("func_ranges", {}),
        lines,
        hops=1
    )
    relevant.update(expanded_lines)

    # Always include imports
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            relevant.add(node.lineno - 1)

    if not relevant:
        return "\n".join(lines)

    return "\n".join(lines[i] for i in sorted(relevant) if i < len(lines))


def _chunk_performance(tree: ast.AST, lines: list, call_graph: dict) -> str:
    """
    Send performance agent only:
    - functions containing loops or comprehensions
    - PLUS their 1-hop callees (a loop calling another function that also loops)
    """
    relevant      = set()
    flagged_funcs = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            has_loop = any(
                isinstance(child, (ast.For, ast.While, ast.ListComp,
                                   ast.DictComp, ast.SetComp, ast.GeneratorExp))
                for child in ast.walk(node)
                if child is not node
            )
            if has_loop:
                flagged_funcs.append(node.name)

    expanded_lines = expand_with_dependencies(
        flagged_funcs,
        call_graph,
        call_graph.get("func_ranges", {}),
        lines,
        hops=1
    )
    relevant.update(expanded_lines)

    if not relevant:
        return "\n".join(lines)

    return "\n".join(lines[i] for i in sorted(relevant) if i < len(lines))


def _chunk_docs(tree: ast.AST, lines: list) -> str:
    """
    Docs agent only needs function/class signatures + first 5 lines.
    No dependency expansion needed — just checking for docstrings.
    """
    relevant = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno - 1
            relevant.update(range(max(0, start), min(len(lines), start + 6)))

    return "\n".join(lines[i] for i in sorted(relevant) if i < len(lines)) if relevant else "\n".join(lines)


def _chunk_imports(tree: ast.AST, lines: list) -> str:
    """Dependency agent only needs import statements."""
    relevant = {n.lineno - 1 for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    return "\n".join(lines[i] for i in sorted(relevant) if i < len(lines)) if relevant else ""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Orchestrator summary
# ─────────────────────────────────────────────────────────────────────────────

def build_orchestrator_summary(structure: dict, flags: dict, call_graph: dict) -> str:
    """
    Compact text summary for the orchestrator.
    Now also includes call graph highlights — which functions call which dangerous things.
    """
    s     = structure
    graph = call_graph.get("graph", {})
    out   = []

    out.append(f"Lines: {s['line_count']}")

    if s["functions"]:
        fn_list = ", ".join(f["name"] for f in s["functions"])
        out.append(f"Functions ({len(s['functions'])}): {fn_list}")

    if s["classes"]:
        out.append(f"Classes: {', '.join(c['name'] for c in s['classes'])}")

    if s["imports"]:
        out.append(f"Imports: {', '.join(s['imports'])}")

    if s["duplicate_functions"]:
        out.append(f"WARN duplicate functions: {', '.join(s['duplicate_functions'])}")

    if s["dangerous_calls"]:
        calls = ", ".join(f"{d['call']} (line {d['line']})" for d in s["dangerous_calls"])
        out.append(f"WARN dangerous calls: {calls}")

    if s["sql_strings"]:
        out.append(f"WARN raw SQL strings: {len(s['sql_strings'])}")

    if s["fstring_lines"]:
        out.append(f"WARN f-strings (injection risk) at lines: {s['fstring_lines']}")

    if s["has_nested_loops"]:
        out.append(f"WARN nested loops at lines: {s['nested_loop_lines']}")

    # Call graph highlights — show indirect dangers
    indirect_dangers = []
    for caller, callees in graph.items():
        for callee in callees:
            short = callee.split(".")[-1]
            if any(d in callee for d in DANGEROUS_CALLS):
                indirect_dangers.append(f"{caller}() → {callee}()")
    if indirect_dangers:
        out.append(f"WARN indirect dangerous calls: {'; '.join(indirect_dangers)}")

    if s["functions_without_docstring"]:
        out.append(f"Missing docstrings: {', '.join(s['functions_without_docstring'])}")

    if s["functions_without_types"]:
        out.append(f"Missing type hints: {', '.join(s['functions_without_types'])}")

    out.append(f"Complexity score: {s['complexity_score']}")
    out.append(f"Pre-selected agents: {', '.join(flags['pre_selected'])}")

    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def preprocess(code: str) -> dict:
    """
    Full pre-processing pipeline. Call this before any LLM.

    Returns:
        tree                 — ast.AST or None
        structure            — all extracted facts
        call_graph           — function → callees map + line ranges
        flags                — which agents are needed
        orchestrator_summary — compact text (replaces raw code for orchestrator)
        chunks               — { agent: focused_code_string }
        syntax_error         — bool
    """
    tree = safe_parse(code)

    if tree is None:
        empty_structure = {
            "line_count": len(code.splitlines()), "functions": [], "classes": [],
            "imports": [], "duplicate_functions": [], "has_loops": False,
            "has_nested_loops": False, "nested_loop_lines": [], "has_try_except": False,
            "complexity_score": 0, "dangerous_calls": [], "sql_strings": [],
            "fstring_lines": [], "functions_without_docstring": [],
            "functions_without_types": []
        }
        return {
            "tree":                 None,
            "structure":            empty_structure,
            "call_graph":           {"graph": {}, "func_ranges": {}},
            "flags":                {"pre_selected": ["analyzer"]},
            "orchestrator_summary": f"Lines: {len(code.splitlines())}\nWARN syntax error — AST unavailable",
            "chunks":               {"analyzer": code},
            "syntax_error":         True,
        }

    structure  = extract_structure(code, tree)
    call_graph = build_call_graph(tree)
    flags      = quick_flags(structure["imports"], structure)
    summary    = build_orchestrator_summary(structure, flags, call_graph)

    chunks = {
        agent: extract_chunk_for_agent(code, tree, agent, call_graph)
        for agent in flags["pre_selected"]
    }

    return {
        "tree":                 tree,
        "structure":            structure,
        "call_graph":           call_graph,
        "flags":                flags,
        "orchestrator_summary": summary,
        "chunks":               chunks,
        "syntax_error":         False,
    }
