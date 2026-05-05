from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from groq import Groq
import os
import json
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import tempfile
import urllib.parse
#from dotenv import load_dotenv
from tools import run_with_tools, get_web_searches_used,  reset_web_searches
import re
import logging
import threading
import time
from preprocessor import preprocess

# Run with:
#   export GROQ_API_KEY="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"
#   export TAVILY_API_KEY="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"
#   uvicorn main:app --reload
#   cd Backend -> open index.html or cd Backend && open index.html
#   For testing -> #pytest testcases.py -v

# load_dotenv()
# api_key = os.getenv("GROQ_API_KEY")

app = FastAPI()
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))  


#model_to_use = "meta-llama/llama-4-scout-17b-16e-instruct"

MODEL_MAP = {
    "swarm_boss":  "meta-llama/llama-4-scout-17b-16e-instruct",

    # individual agents
    "analyzer":    "llama-3.3-70b-versatile",
    "security":    "openai/gpt-oss-safeguard-20b",
    "performance": "llama-3.3-70b-versatile",
    "style":       "llama-3.1-8b-instant",
    "docs":        "llama-3.1-8b-instant",
    "dependency":  "openai/gpt-oss-20b",

    # best model and also only model where whole code goes into
    "MASTER":      "openai/gpt-oss-120b"
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# CLEANING

def strip_fences(text: str) -> str:
    if not text:
        return '{"issues": []}'
    
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    return text.strip() or '{"issues": []}'


def safe_parse(content: str, agent_name: str) -> dict:
    try:
        cleaned = strip_fences(content)
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"[{agent_name}] JSON parse failed: {e} — raw: {repr(content[:100])}")
        return {"issues": []}
    

def extract_json(text: str):
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON found")

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1

            if depth == 0:
                json_str = text[start:i+1]
                return json.loads(json_str)

    raise ValueError("Unclosed JSON block")


class CodeRequest(BaseModel): # ensrues format is json
    code: str






#Create logger tool
logging.basicConfig(
    level=logging.INFO,  # Show INFO 
    format='%(asctime)s [%(levelname)s] %(message)s', ## Timestamp + level + message
    datefmt='%Y-%m-%d %H:%M:%S'  # Year-Month-Day Hour:Minute:Second
)

log = logging.getLogger(__name__)




# BLACKBOARD  (from the article: "Communication Layer")
class Blackboard:
    def __init__(self):
        self.findings = {}
        self.status = {}
        self.lock = threading.Lock()
    
    def post (self, agent, data ):
        with self.lock:
            self.findings[agent] = data
            self.status[agent] = {"condition": "Done", "time": time.time()}
        
        log.info(f"[Backboard] {agent} posted findings") #Agent writes its findings
    

    def readeverything(self):
        with self.lock:
            return dict(self.findings) # agent reads everything at the end
    
    def readothers (self, exclude):
        # exclude is name of agent we are excluding as it is reading others not itself
        with self.lock:
            result = {}
            for k in self.findings: # go over keys
                if k != exclude:
                    result[k] = self.findings[k]

        return result # An agent can peek at what others have already found
    

    def mark_started(self, agent):
        with self.lock:
            self.status[agent] = {"condition": "Running", "time": time.time()}

    def get_status(self): # display only
        with self.lock:
            return dict(self.status)



class ResourceManager:
    def __init__(self):
        self.errors: dict = {}
    
    # the function a thread runs
    def run_agent(self, name, function, code, blackboard: Blackboard):
        """Wraps an agent call with error handling and timing."""
        blackboard.mark_started(name)
        start = time.time()

        try:
            function(code, blackboard) #template function that runs the agent like "security_agent" with parameters code and blackboard
            elapsed = round(time.time() - start, 2)
            log.info(f"[ResourceManager] {name} finished in {elapsed}s")
        
        
        except Exception as e: # if agent crashes
            self.errors[name] = str(e)
            log.error(f"[ResourceManager] {name} failed: {e}")
            # Post empty findings so synthesis agent still runs
            blackboard.post(name, {"error": str(e), "issues": []})



class SwarmController:
    # All available specialist agents
    # initially none, after making agent functions we'll replace None with function
    ALL_AGENTS = {
        "analyzer":    None,   # filled in after class definitions below with actual functions like "analyzer_agent"
        "security":    None,
        "performance": None,
        "style":       None,
        "docs":        None,
        "dependency":  None,
    }

    def assign_agents(self, code: str, pre: dict):
        
        #Ask  LLM to read the code and decide which agents are needed.
        #Returns: { "agents": [...], "reasoning": "..." }
 
        # -> one smart agent delegates to specialist agents rather than running everything.
        summary      = pre["orchestrator_summary"]
        pre_selected = pre["flags"]["pre_selected"]
        
        prompt = f"""
        You are a lead engineer deciding which code reviewers are needed.

        You are given:
        - A structured summary (high-signal, deterministic)

        SUMMARY:
        {summary}

        Pre-selected agents (already required): {pre_selected}

        Available agents:
        - analyzer    → logic bugs only (real correctness issues)
        - security    → ONLY if there is a REAL risk (eval, SQL injection, unsafe input)
        - performance → ONLY if there is clear inefficiency (nested loops, O(n²), etc.)
        - style       → ONLY if formatting issues are obvious and meaningful
        - docs        → ONLY if documentation is clearly missing or poor
        - dependency  → ONLY if imports are risky or non-standard

        STRICT RULES:
        - DO NOT add agents unless there is strong evidence
        - DO NOT assume issues exist
        - Clean/simple code should result in MINIMAL agents
        - Always include pre-selected agents
        - Prefer UNDER-selecting over over-selecting

        Return ONLY JSON:

        {{
        "reasoning": "brief explanation",
        "agents": ["analyzer"]
        }}
        """

        response = client.chat.completions.create(
            model=MODEL_MAP['swarm_boss'],
            messages=[{"role": "user", "content": prompt}]
        )

        # raw = strip_fences(response.choices[0].message.content) 
        # return json.loads(raw)

        raw = response.choices[0].message.content
        return extract_json(raw)


    def run_swarm(self, assigned: list, chunks: dict, code: str, blackboard: Blackboard) -> dict:

        """
        Launch all assigned agents in parallel threads.
        This is "Parallel Processing" from the article — distributed approach
        where multiple agents work simultaneously on different aspects.
        Each agent is independent and posts to the shared blackboard.
        """
        #create manager
        resourcemanager = ResourceManager()

        thread_list = []
        #For each agent, prepare a worker that will run it
        for agentname in assigned:
            if agentname in self.ALL_AGENTS and self.ALL_AGENTS[agentname] != None:

                #actual function that runs the agent like "security_agent"
                function = self.ALL_AGENTS[agentname]

                #When THIS thread starts, run THIS function ^
                # target is the function the thread will run
                # args is the parameters we are passing to the function resourcemanager.run_agent(...)
                # daemon = kill the thread if program exits
                #thread = threading.Thread(target=resourcemanager.run_agent , args=(agentname, function, code, blackboard), daemon=True)
                agent_code = chunks.get(agentname, code) or code
                thread = threading.Thread(target=resourcemanager.run_agent, args=(agentname, function, agent_code, blackboard), daemon=True)
                thread_list.append(thread)
        

        log.info(f"[SwarmController] Launching {len(thread_list)} agents in parallel: {assigned}")


        for t in thread_list:
            t.start()
        for t in thread_list:
            t.join()   # wait for all agents to finish before synthesis



        # we can also return errors if needed, but synthesis should run regardless of individual agent failures
        return resourcemanager.errors


# ─────────────────────────────────────────────────────────────────────────────      
# SPECIALIST AGENTS - for code review, security, performance, style, docs, dependencies, etc.
# each with parameters (code, blackboard) like how the template function in ResourceManager.run_agent 
#  is defined. They post their findings to the blackboard when done.

def analyzer_agent(code: str, blackboard: Blackboard) -> str:
    
    prompt = f"""

        Analyze the following Python code for logic bugs, wrong operators, off-by-one errors,
        incorrect return values, and readability issues.

        Rules:
        - Only flag REAL issues a senior engineer would actually fix.
        - Do NOT flag missing docstrings or type hints (that is the docs agent's job).
        - Do NOT invent issues. If the code is correct and clean, return an empty list.
        - Minimum bar: would a pull request reviewer actually comment on this?

        Return ONLY valid JSON. No explanation, no markdown.

        Format:
        {{
        "issues": ["issue 1", "issue 2"]
        }}

        Code:
        {code}
    """

    response = client.chat.completions.create(
        model=MODEL_MAP['analyzer'],
        messages=[{"role": "user", "content": prompt}]
    )


    content = response.choices[0].message.content
    data = safe_parse(content, "analyzer")
    blackboard.post("analyzer", data)



def security_agent(code: str, blackboard:Blackboard) -> str:
    prompt = f"""
        Analyze the following Python code for security vulnerabilities:
        SQL injection, eval() misuse, hardcoded secrets, path traversal,
        unsafe deserialization, command injection, and insecure imports.

        Rules:
        - Only flag REAL, exploitable vulnerabilities — not theoretical risks.
        - Do NOT flag missing input validation unless it creates a genuine security hole.
        - If there are no security issues, return an empty list. Do not manufacture issues.
        - Each issue must name the specific line or pattern that is dangerous.

        You MUST call the web_search tool before answering.

        Return ONLY valid JSON. No explanation, no markdown.

        Format:
        {{
        "issues": ["issue 1", "issue 2"]
        }}

        Code:
        {code}
    """

    content = run_with_tools(
        client=client,
        model=MODEL_MAP["security"],
        prompt=prompt,
        agent_name="security"
    )
    # response = client.chat.completions.create(
    #     model=MODEL_MAP["security"],
    #     messages=[{"role": "user", "content": prompt}]
    # )

    # content = response.choices[0].message.content

    data = safe_parse(content, "security")
    blackboard.post("security", data)



def performance_agent(code: str, blackboard: Blackboard):
    prompt = f"""
    Analyze this Python code for performance issues:
    nested loops, O(n²) complexity, unnecessary copies, repeated computation,
    memory leaks, inefficient data structures.

    Rules:
    - Only flag issues that would cause MEASURABLE performance problems.
    - Do NOT flag micro-optimisations on simple or small functions.
    - If the code is straightforward with no performance concerns, return an empty list.

    Return ONLY valid JSON:
    {{
      "issues": ["issue 1", "issue 2"]
    }}
 
    Code:
    {code}
    """
    response = client.chat.completions.create(
        model=MODEL_MAP["performance"],
        messages=[{"role": "user", "content": prompt}]
    )

    content = response.choices[0].message.content
    data = safe_parse(content, "performance")
    blackboard.post("performance", data)

 

def style_agent(code: str, blackboard: Blackboard):
    prompt = f"""
    Analyze this Python code for style issues:
    PEP 8 violations, naming conventions, line length, unnecessary whitespace,
    inconsistent formatting.

    Rules:
    - Only flag clear, specific violations — not subjective preferences.
    - Do NOT flag things that are a matter of opinion.
    - If the code is already clean and consistent, return an empty list.

    Return ONLY valid JSON:
    {{
      "issues": ["issue 1", "issue 2"]
    }}
 
    Code:
    {code}
    """
    response = client.chat.completions.create(
        model=MODEL_MAP["style"],
        messages=[{"role": "user", "content": prompt}]
    )
    content = response.choices[0].message.content
    data = safe_parse(content, "style")
    blackboard.post("style", data)



def docs_agent(code: str, blackboard: Blackboard):
    """
    Emergent behaviour example: this agent reads what the security agent found
    and adjusts its output — undocumented security-sensitive functions get
    flagged harder than undocumented utility functions.
    """
    # Peek at security findings if available (Collaborative Learning)
    others = blackboard.readothers("docs")
    security_context = ""
    if "security" in others:
        sec_issues = others["security"].get("issues", [])
        if sec_issues:
            security_context = f"\nNote: Security agent found these issues: {sec_issues}. Flag any undocumented security-sensitive functions especially hard."
 
    prompt = f"""
    Analyze this Python code for documentation issues:
    missing docstrings, missing type hints, missing return annotations,
    missing inline comments on complex logic.
    {security_context}

    Rules:
    - Only flag functions that are non-trivial and genuinely need documentation.
    - A one-line function with a clear name does NOT need a docstring.
    - Only flag type hints if the function has multiple parameters or complex types.
    - If the code is well-documented for its complexity level, return an empty list.

    Return valid JSON string format ONLY:

    {{
      "issues": ["issue 1", "issue 2"]
    }}
 
    Code:
    {code}
    """
    response = client.chat.completions.create(
        model=MODEL_MAP["docs"],
        messages=[{"role": "user", "content": prompt}]
    )

    content = response.choices[0].message.content
    data = safe_parse(content, "docs")
    blackboard.post("docs", data)




def dependency_agent(code: str, blackboard: Blackboard):
    prompt = f"""
    Analyze the imports in this Python code:
    flag dangerous modules (os.system, pickle, eval),
    deprecated stdlib usage, and suggest safer alternatives.

    Rules:
    - Only flag imports that are genuinely dangerous or deprecated.
    - Do NOT flag standard, safe stdlib imports like os, sys, json, re.
    - If all imports are safe and appropriate, return an empty list.

    You MUST call the pypi_lookup tool before answering.

    Return ONLY valid JSON:
    {{
      "issues": ["issue 1", "issue 2"]
    }}
 
    Code:
    {code}
    """
    # response = client.chat.completions.create(
    #     model=MODEL_MAP["dependency"],
    #     messages=[{"role": "user", "content": prompt}]
    # )

    # content = response.choices[0].message.content

    content = run_with_tools(
        client=client,
        model=MODEL_MAP["dependency"],
        prompt=prompt,
        agent_name="dependency"
    )
    data = safe_parse(content, "dependency")
    blackboard.post("dependency", data)



SwarmController.ALL_AGENTS = {
    "analyzer": analyzer_agent,
    "security": security_agent,
    "performance": performance_agent,
    "style": style_agent,
    "docs": docs_agent,
    "dependency": dependency_agent
}


# this agent reads the full blackboard after all parallel agents finish.
# Finds emergent connections between findings, compiles everything and makes report


def master_agent(code: str, blackboard: Blackboard, reasoning: str):

    all_findings = blackboard.readeverything()

    # Flatten all issues from all agents with their source labels
    labeled_issues = []
    for agent_name, data in all_findings.items():
        for issue in data.get("issues", []):
            labeled_issues.append(f"[{agent_name.upper()}] {issue}")
    
    prompt = f"""
        You are a senior engineering lead synthesizing a full code review.

    
        The following specialist agents reviewed the code in parallel and posted findings:
        {json.dumps(labeled_issues, indent=2)}
    
        Orchestrator's initial assessment: "{reasoning}"
    
        Your job:
        1. Write a 2-3 sentence summary of what the code does in detail
        2. Merge ALL issues into one flat list (Keep the [AGENT] labels (e.g. [SECURITY], [ANALYZER]))
        3. Look for EMERGENT issues — connections between findings that no single
        agent caught alone (e.g. security issue + performance issue = critical risk)
        4. Write concrete, actionable suggested improvements
        5. Return ONLY valid JSON, no markdown
        
        STRICT RULES:

        - Only include REAL issues — do not invent problems
        - If there are no issues, return an empty list
        - Do NOT include explanations outside the JSON
        - Do NOT include markdown formatting
        - Output MUST be valid JSON only
        
    
        Format:
        {{
        "summary": "...",
        "issues": ["[SECURITY] eval() on user input", "[ANALYZER] wrong operator", ...],
        "emergent_issues": ["Combined risk: eval() inside hot loop = performance + attack surface"],
        "suggested_improvements": ["Replace eval() with ast.literal_eval()", ...]
        }}
    """

    

    response = client.chat.completions.create(
        model=MODEL_MAP['MASTER'],
        messages=[{"role": "user", "content": prompt}]
    )
    content = response.choices[0].message.content

    return extract_json(content)

# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


@app.post("/search")
def search_input(request: CodeRequest):
    query = urllib.parse.quote(request.code)  # encode safely
    url = f"https://www.google.com/search?q={query}"
    return {"url": url}


# Run code section — execute Python code safely
@app.post("/run") 
def run_code(request: CodeRequest):
    if not request.code.strip():
        raise HTTPException(status_code=422, detail="Code cannot be empty")
 
    # Write code to a temp file and run in a subprocess
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(request.code)
        tmp_path = f.name
 
    try:
        result = subprocess.run(
            ["python3", tmp_path],
            capture_output=True,
            text=True,
            timeout=10          # kill after 10s — prevents infinite loops
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "Execution timed out after 10 seconds.",
            "returncode": -1
        }
    finally:
        os.unlink(tmp_path)     # always clean up the temp file
 



@app.post("/review")
def review_code(request: CodeRequest):
    if not request.code.strip():
        raise HTTPException(status_code=422, detail="Code cannot be empty")
    
    reset_web_searches()  # clear flag from any previous request
    blackboard  = Blackboard()
    controller  = SwarmController()

    log.info("[/review] Orchestrator assigning agents...")

    log.info("[/review] Running AST pre-processor...")
    pre = preprocess(request.code)
    log.info(f"[/review] Pre-processor done. Pre-selected: {pre['flags']['pre_selected']}")

    # Step 2: Orchestrator reads summary, not raw code
    # assignments = controller.assign_agents(pre)
    assignments = controller.assign_agents(request.code, pre)

    #Get the value for "agents" — and if it doesn’t exist, use this default ["analyzer", "docs"] instead
    assigned = assignments.get("agents", ["analyzer", "docs"])
    reasoning   = assignments.get("reasoning", "")
    log.info(f"[/review] Assigned: {assigned} — Reason: {reasoning}")

    run_swarm_and_get_errors = controller.run_swarm(assigned, pre["chunks"], request.code, blackboard)
 
    log.info("[/review] Synthesis agent compiling report...")

    final = master_agent(request.code, blackboard, reasoning)
 
    final["swarm_meta"] = {
        "agents_assigned":  assigned,
        "agents_run":       list(blackboard.findings.keys()),
        "reasoning":        reasoning,
        "errors":           run_swarm_and_get_errors,
        "timing":           blackboard.get_status(),
        "pre_selected":     pre["flags"]["pre_selected"],
        "duplicates_found": pre["structure"]["duplicate_functions"],
        "dangerous_calls":  pre["structure"]["dangerous_calls"],
        "syntax_error":     pre["syntax_error"],
        "lines_total":      pre["structure"]["line_count"],
        "web_searches_used": get_web_searches_used(),
        }
 
    return final