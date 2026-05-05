//store strings -> these are default functions to test out
const PRESETS = { 
  simple:   `def add(a, b):\n    return a + b \n\nprint(add(1,2))` ,
  security: `def calculate(expr):\n    return eval(expr)`,
  sql:      `def get_user(username):\n    query = f"SELECT * FROM users WHERE username = '{username}'"\n    db.execute(query)`,
  logic:    `def multiply(a, b):\n    # BUG: should be a * b\n    return a + b \n\nprint(multiply(5,2))`,
  nested:   `def check(a, b, c):\n    if a:\n        if b:\n            if c:\n                return True\n            else:\n                return False\n        else:\n            return False\n    else:\n        return False`
};


// This makes the python code editor interface 
// change editor settings based on requirements 
const editor = CodeMirror(document.getElementById('editorMount'), {
  value:             PRESETS.simple,
  mode:              'python',
  theme:             'one-dark',
  lineNumbers:       true,
  autoCloseBrackets: true,
  matchBrackets:     true,
  styleActiveLine:   true,
  indentUnit:        4,
  tabSize:           4,
  indentWithTabs:    false,
  extraKeys: {
    'Tab':        cm => cm.execCommand('insertSoftTab'),
    'Ctrl-Enter': runReview, // run code review function when user clicks cmd/ctrl + enter
    'Cmd-Enter':  runReview,
    'Ctrl-/':     cm => cm.execCommand('toggleComment'),
    'Cmd-/':      cm => cm.execCommand('toggleComment'),
  }
});

editor.on('change', () => {
  const n = editor.lineCount(); // this function gets number of lines and displays
  //Display the number of lines, and add ‘s’ if it’s not exactly 1.
  document.getElementById('lineCount').textContent = `${n} line${n !== 1 ? 's' : ''}`; //updates UI
});



function loadPreset(name) {
  editor.setValue(PRESETS[name]);
  editor.focus(); // Moves the cursor into the editor. Lets the user start typing immediately without clicking.
}


// ─────────────────────────────────────────
// RUN CODE
// ─────────────────────────────────────────
 
async function runCode() {
  const code = editor.getValue();
  if (!code.trim()) return;
 
  const btn      = document.getElementById('runBtn');
  const output   = document.getElementById('runOutput');
  const runPanel = document.getElementById('runPanel');
 
  btn.disabled = true;
  btn.textContent = 'Running...';
  runPanel.style.display = 'block';
  output.className = 'run-output loading';
  output.textContent = '$ python3 script.py';
 
  try {
    const response = await fetch('http://127.0.0.1:8000/run', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ code })
    });
 
    const data = await response.json();
 
    if (data.returncode === -1) {
      // Timeout
      output.className = 'run-output error';
      output.textContent = `⏱ ${data.stderr}`;
    } else if (data.returncode !== 0) {
      // Runtime error
      output.className = 'run-output error';
      output.textContent = data.stderr || 'Unknown error';
    } else {
      // Success
      output.className = 'run-output success';
      output.textContent = data.stdout || '(no output)';
    }
 
  } catch (err) {
    output.className = 'run-output error';
    output.textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run';
  }
}



//RUN REVIEW
async function runReview() {

  const code = editor.getValue(); // fetch text from whatever user put in code editor

  if (!code.trim()) return; // If the user didn’t actually write any code (excluding whitespace), stop 

  const btn      = document.getElementById('submitBtn'); // get submit button to disable/enable the 'btn'
  const spinner  = document.getElementById('spinner'); // show loading -. display 
  const btnLabel = document.getElementById('btnLabel'); // Get the text inside the button, change to 'Analyzing'

  btn.disabled = true; // disable submit button when clicked so user cant click when loading answer
  spinner.style.display = 'block';
  btnLabel.textContent  = 'Analyzing...';

  document.getElementById('placeholderState').style.display = 'none';
  document.getElementById('resultArea').style.display       = 'none';
  document.getElementById('errorBox').style.display         = 'none';
  document.getElementById('statsBar').style.display         = 'none';
  document.getElementById('loadingState').style.display     = 'flex';

  // when user submits create a simple animated loading sequence that cycles through “agents” 
  // and updates the UI every second

  const agentLabels = ['agentA', 'agentS', 'agentR']; // UI purposes our 3 agents
  // 3 things to display 1 for each agent
  const loadingMsgs = ['Running Analyzer...', 'Running Security Agent...', 'Compiling Report...'];
  
  let agentIdx = 0;
  const agentInterval = setInterval(() => {
    agentLabels.forEach(id => document.getElementById(id).classList.remove('active')); //Clears previous info
    if (agentIdx < agentLabels.length) {
      document.getElementById(agentLabels[agentIdx]).classList.add('active'); //Highlights the current 'agent' visually.
      document.getElementById('loadingLabel').textContent = loadingMsgs[agentIdx]; //Changes the 'message' shown to the user.
      agentIdx++; // increment index in loadingMsgs to go to next agent lebel/ msg
    }
  }, 1000); //Runs the function every 1000 ms


  const startTime = Date.now(); //Used to calculate how long review took (Issues:3 Fixes:1 Time:2.2s)

  //API call
  try {
    const response = await fetch('http://127.0.0.1:8000/review', { //sends HTTP request
      method:  'POST', //You’re sending data (not just fetching)
      headers: { 'Content-Type': 'application/json' }, //Tells the server: “I’m sending JSON data.”
      body:    JSON.stringify({ code }) // stringyfy the code variable storing the user's input 
    }); 

    // e.g. { "code": "def add(a, b): return a + b"}

    clearInterval(agentInterval); //Stops your “agent progressing” animation once a response arrives

    //Error handling
    if (!response.ok) { 
      const err = await response.json();
      throw new Error(err.detail || `HTTP ${response.status}`);
    }

    const data    = await response.json(); // get info

    console.log(data)
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1); // how much tipe passed since 



    // Summary
    document.getElementById('summaryBox').textContent = data.summary || '—';

    // Render per-agent findings grouped by agent
    const agentFindings = document.getElementById('agentFindings');
    agentFindings.innerHTML = '';

    // Agent display config — label + colour class
    const AGENT_CONFIG = {
      analyzer:    { label: 'Analyzer',    cls: 'analyzer'    },
      security:    { label: 'Security',    cls: 'security'    },
      performance: { label: 'Performance', cls: 'performance' },
      style:       { label: 'Style',       cls: 'style'       },
      docs:        { label: 'Docs',        cls: 'docs'        },
      dependency:  { label: 'Dependency',  cls: 'dependency'  },
    };

    // Build a map of agent → issues from the flat issues list
    // Issues come back as "[ANALYZER] some issue" or plain text
    const byAgent = {};
    (data.issues || []).forEach(issue => {
      if (!issue || typeof issue !== 'string') return; // guard against null/empty
      const match = issue.match(/^\[([A-Z]+)\]\s*(.+)$/);
      if (match) {
        const key = match[1].toLowerCase();
        if (!byAgent[key]) byAgent[key] = [];
        byAgent[key].push(match[2]);
      } else {
        if (!byAgent['general']) byAgent['general'] = [];
        byAgent['general'].push(issue);
      }
    });

    // Render each agent group
    const agentOrder = ['analyzer', 'security', 'performance', 'style', 'docs', 'dependency', 'general'];
    agentOrder.forEach(key => {
      if (!byAgent[key] || byAgent[key].length === 0) return;

      const cfg = AGENT_CONFIG[key] || { label: key, cls: 'analyzer' };

      // Agent group wrapper
      const group = document.createElement('div');
      group.className = 'agent-group';

      // Header with badge
      const header = document.createElement('div');
      header.className = 'agent-group-header';
      header.innerHTML = `<span class="agent-group-badge badge-${cfg.cls}">${cfg.label}</span>
                          <span class="agent-group-count">${byAgent[key].length} issue${byAgent[key].length !== 1 ? 's' : ''}</span>`;
      group.appendChild(header);

      // Issues under this agent
      const ul = document.createElement('ul');
      ul.className = 'issue-list';
      byAgent[key].forEach((issue, i) => {
        const li = document.createElement('li');
        li.className = 'issue-item';
        li.textContent = issue;
        li.style.animationDelay = `${i * 0.06}s`;
        ul.appendChild(li);
      });
      group.appendChild(ul);
      agentFindings.appendChild(group);
    });

    // Empty state — show green tick if no issues found
    if (agentFindings.innerHTML === '') {
      agentFindings.innerHTML = '<p class="no-issues">✓ No issues found</p>';
    }

    // Emergent issues
    const emergentSection = document.getElementById('emergentSection');
    const emergentList = document.getElementById('emergentList');
    const emergentIssues = data.emergent_issues || [];
    if (emergentIssues.length > 0) {
      emergentList.innerHTML = '';
      emergentIssues.forEach((item, i) => {
        if (!item || typeof item !== 'string') return; // guard against null/empty
        const li = document.createElement('li');
        li.className = 'emergent-item';
        li.textContent = item.replace(/^\[[A-Z]+\]\s*/, '');
        li.style.animationDelay = `${i * 0.06}s`;
        emergentList.appendChild(li);
      });
      emergentSection.style.display = 'block';
    } else {
      if (emergentSection) emergentSection.style.display = 'none';
    }

    // Improvements
    const improvList = document.getElementById('improvementList');
    improvList.innerHTML = '';
    (data.suggested_improvements || []).forEach((item, i) => {
      const li = document.createElement('li');
      li.className = 'improvement-item';
      li.textContent = item;
      li.style.animationDelay = `${i * 0.06}s`;
      improvList.appendChild(li);
    });

    // Swarm meta — orchestrator reasoning + agents deployed
    const meta = data.swarm_meta;
    if (meta) {
      document.getElementById('swarmReasoning').textContent = meta.reasoning || '';
      const agentTags = document.getElementById('agentTags');
      agentTags.innerHTML = '';
      (meta.agents_run || []).forEach(name => {
        const span = document.createElement('span');
        span.className = `agent-tag agent-${name}`;
        span.textContent = (AGENT_CONFIG[name] || {label: name}).label;
        agentTags.appendChild(span);
      });
      document.getElementById('swarmMeta').style.display = 'block';

      // Web search indicator
      const searches = meta.web_searches_used ;
      const existingBadge = document.getElementById('webSearchBadge');
      if (existingBadge) existingBadge.remove();

      if (searches == true) {
        const badge = document.createElement('div');
        badge.id = 'webSearchBadge';
        badge.className = 'web-search-badge';
        // Build tooltip text: one line per search
        //const details = searches.map(s => `${s.agent}: "${s.query}"`).join('\n');
        badge.title = 'Web search was used';
        badge.innerHTML = '🌐 Web results used';
        document.getElementById('swarmMeta').appendChild(badge);
      }
    }

    //Get stats
    document.getElementById('statIssues').textContent = (data.issues || []).length;
    document.getElementById('statFixes').textContent  = (data.suggested_improvements || []).length;
    document.getElementById('statTime').textContent   = `${elapsed}s`;

    //Switch UI from loading → results
    document.getElementById('loadingState').style.display = 'none';
    document.getElementById('resultArea').style.display   = 'block';
    document.getElementById('statsBar').style.display     = 'flex';

    //Error handling
  } catch (err) {
    clearInterval(agentInterval); //Stops animation
    document.getElementById('loadingState').style.display = 'none'; //hides loading UI

    //Shows error message to user
    const errBox = document.getElementById('errorBox');
    errBox.style.display = 'block';
    errBox.textContent   = `Error: ${err.message}`;

  } finally {
    btn.disabled = false; //Re-enable button
    spinner.style.display = 'none'; //Hide loade=ing animation
    btnLabel.textContent  = 'Run Review'; //Reset button text
  } 
}


async function search() {
  // Get selected text OR fallback to full editor content
  const selected = editor.getSelection();
  const input = selected || editor.getValue();

  if (!input.trim()) return;

  const url = `https://www.google.com/search?q=${encodeURIComponent(input)}`;
  window.open(url, "_blank");
}