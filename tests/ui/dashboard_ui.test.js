// Uso: node dashboard_ui.test.js <fixtures.json>
// Richiede jsdom (npm install dentro tests/ui). Lanciato da tests/test_dashboard_ui.py.
const { JSDOM } = require('jsdom');
const fs = require('fs');
const nodePath = require('path');
const html = fs.readFileSync(nodePath.join(__dirname, '..', '..', 'web', 'templates', 'dashboard.html'), 'utf8');
const fx = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const EVIL = '<img src=x onerror="window.__xss=1">Server A';
fx.me.guilds.forEach((g) => { if (g.id === fx.GA) g.name = EVIL; });

let fails = 0;
function check(name, cond, extra) {
  if (!cond) fails++;
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (!cond && extra !== undefined ? '  [' + extra + ']' : ''));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function openPage(url, handler) {
  const calls = [];
  const errors = [];
  const dom = new JSDOM(html, {
    url, runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(win) {
      win.scrollTo = () => {};
      win.confirm = () => true;
      win.fetch = (p, init) => {
        init = init || {};
        calls.push({ path: p, method: init.method || 'GET', headers: init.headers || {}, body: init.body });
        const r = handler(p, init) || { status: 404, body: { detail: 'nf' } };
        return Promise.resolve({ status: r.status, ok: r.status >= 200 && r.status < 300, text: () => Promise.resolve(JSON.stringify(r.body)) });
      };
      win.addEventListener('error', (e) => errors.push(e.message));
    },
  });
  await sleep(60);
  return { dom, win: dom.window, doc: dom.window.document, calls, errors };
}
const text = (doc) => doc.getElementById('app').textContent;
async function go(page, hash) { page.win.location.hash = hash; await sleep(80); }

(async () => {
  // ---- 1. non autenticato: schermata di login, con errore
  let p = await openPage('https://testserver/dashboard?error=denied', (path) => path === '/api/me' ? { status: 401, body: {} } : null);
  check('login: mostra "Accedi con Discord"', text(p.doc).includes('Accedi con Discord'));
  check('login: link a /auth/login', p.doc.querySelector('a.btn-discord').getAttribute('href') === '/auth/login');
  check('login: messaggio di errore "denied"', text(p.doc).includes('Accesso annullato'));
  check('login: errore tolto dall\'URL', !p.win.location.search.includes('error'));
  check('login: barra utente nascosta', p.doc.getElementById('topRight').hidden === true);
  check('login: nessun errore JS', p.errors.length === 0, p.errors.join('|'));

  // ---- 2. utente normale
  const state = { saved: [] };
  const handler = (path, init) => {
    const m = init.method || 'GET';
    if (path === '/api/me') return { status: 200, body: fx.me };
    if (path === `/api/guilds/${fx.GA}/overview`) return { status: 200, body: fx.overview };
    if (path === `/api/guilds/${fx.GA}/settings` && m === 'GET') return { status: 200, body: fx.settings };
    if (path === `/api/guilds/${fx.GA}/settings` && m === 'PUT') { state.saved.push(JSON.parse(init.body)); return { status: 200, body: { ok: true } }; }
    if (path === `/api/guilds/${fx.GA}/role-menus` && m === 'GET') return { status: 200, body: fx.roles };
    if (path === `/api/guilds/${fx.GA}/role-menus` && m === 'POST') { state.saved.push({ menu: JSON.parse(init.body) }); return { status: 200, body: { ok: true } }; }
    if (path.startsWith(`/api/guilds/${fx.GA}/role-menus/`) && m === 'DELETE') { state.saved.push({ deleted: path }); return { status: 200, body: { ok: true } }; }
    if (path === `/api/guilds/${fx.GA}/tickets` && m === 'GET') return { status: 200, body: fx.tickets };
    if (path === `/api/guilds/${fx.GA}/tickets/panel` && m === 'POST') { state.saved.push({ panel: JSON.parse(init.body) }); return { status: 200, body: { ok: true } }; }
    if (path.endsWith('/close') && m === 'POST') { state.saved.push({ closed: path }); return { status: 200, body: { ok: true } }; }
    if (path === '/api/admin/overview') return { status: 403, body: { detail: 'Solo il proprietario del bot' } };
    if (path === '/auth/logout') return { status: 200, body: { ok: true } };
    return null;
  };
  p = await openPage('https://testserver/dashboard', handler);
  check('server: titolo "I tuoi server"', text(p.doc).includes('I tuoi server'));
  check('server: barra utente con nome', p.doc.getElementById('topRight').textContent.includes('Alice'));
  check('server: nessun pulsante Admin per non-owner', !p.doc.getElementById('topRight').textContent.includes('Admin'));
  const cards = p.doc.querySelectorAll('.srv');
  check('server: 2 server (A col bot, C senza)', cards.length === 2, cards.length);
  const manage = p.doc.querySelector(`a[href="#/g/${fx.GA}"]`);
  check('server: pulsante Gestisci per A', !!manage && manage.textContent === 'Gestisci');
  const invite = [...p.doc.querySelectorAll('.srv a')].find((a) => a.textContent === 'Aggiungi il bot');
  check('server: invito per C verso discord.com con rel noopener', invite && invite.href.startsWith('https://discord.com/oauth2/authorize?') && invite.rel.includes('noopener') && invite.target === '_blank');

  // XSS: il nome del server è testo, non HTML
  const nameEl = [...p.doc.querySelectorAll('.srv-name')].map((e) => e.textContent).join('|');
  check('XSS: nell\'elenco il nome malevolo è testo letterale', nameEl.includes(EVIL), nameEl);
  check('XSS: nessun <img> iniettato e nessun handler eseguito', !p.doc.querySelector('img[src="x"]') && p.win.__xss === undefined);

  // ---- 3. panoramica
  await go(p, `#/g/${fx.GA}`);
  const t = text(p.doc);
  check('panoramica: statistiche', t.includes('Membri') && t.includes('Boost'));
  check('panoramica: grafico SVG con barre', p.doc.querySelectorAll('svg.chart rect').length >= 3, p.doc.querySelectorAll('svg.chart rect').length);
  check('panoramica: classifica con nome', t.includes('Alice') && t.includes('Classifica XP'));
  check('XSS: anche nell\'intestazione del server è testo letterale', p.doc.querySelector('h1').textContent === EVIL && !p.doc.querySelector('img[src="x"]') && p.win.__xss === undefined, p.doc.querySelector('h1').textContent);
  check('panoramica: scheda attiva = Panoramica', p.doc.querySelector('.tab[aria-current="page"]').textContent === 'Panoramica');

  // ---- 4. impostazioni
  await go(p, `#/g/${fx.GA}/settings`);
  check('impostazioni: 8 menu canali/ruoli + 2 verifica', p.doc.querySelectorAll('select').length === 8 + 2, p.doc.querySelectorAll('select').length);
  const roleSel = p.doc.getElementById('f-verified_role');
  const optTexts = [...roleSel.options].map((o) => o.textContent);
  check('impostazioni: ruolo non assegnabile segnalato', optTexts.some((x) => x.includes('Staff') && x.includes('sopra il ruolo del bot')), optTexts.join('|'));
  check('impostazioni: @everyone assente', !optTexts.some((x) => x.includes('everyone')));
  const logSel = p.doc.getElementById('f-log_channel');
  const logOpt = [...logSel.options].find((o) => o.textContent === 'log');
  logSel.value = logOpt.value;
  p.doc.getElementById('f-xp_per_message').value = '25';
  p.doc.getElementById('f-welcome_message').value = 'Ciao {user}!';
  p.doc.getElementById('f-verification_type').value = 'captcha';
  [...p.doc.querySelectorAll('button')].find((b) => b.textContent === 'Salva impostazioni').click();
  await sleep(80);
  const put = p.calls.filter((c) => c.method === 'PUT');
  check('salva: una PUT con header CSRF', put.length === 1 && put[0].headers['X-CSRF-Token'] === fx.me.csrf, JSON.stringify(put.map((x) => x.headers)));
  const sent = state.saved[0] || {};
  check('salva: valori inviati', sent.log_channel === logOpt.value && sent.xp_per_message === 25 && sent.welcome_message === 'Ciao {user}!' && sent.verification_type === 'captcha', JSON.stringify(sent));
  check('salva: campi vuoti inviati come null', sent.verified_role === null && sent.xp_cooldown === null && sent.daily_reward === null && sent.currency_name === null && sent.currency_symbol === null, JSON.stringify(sent));
  check('salva: toast di conferma', p.doc.getElementById('toast').textContent.includes('Impostazioni salvate') && !p.doc.getElementById('toast').hidden);

  // ---- 4b. anteprima del benvenuto e valuta
  const prev = () => p.doc.getElementById('welcome-preview');
  check('anteprima: titolo con nome del server', prev().textContent.includes('Welcome to ' + EVIL), prev().textContent);
  check('anteprima: {user} diventa menzione', prev().querySelectorAll('.mention').length >= 2, prev().innerHTML);
  check('anteprima XSS: il testo malevolo resta testo', !prev().querySelector('img') && p.win.__xss === undefined, prev().innerHTML);
  p.doc.getElementById('f-welcome_message').value = 'Ciao {username} su **{server}**, leggi #generale e {user}';
  p.doc.getElementById('f-welcome_message').dispatchEvent(new p.win.Event('input'));
  check('anteprima: {username} sostituito', prev().textContent.includes('Ciao Mario su'), prev().textContent);
  check('anteprima: **grassetto** reso', [...prev().querySelectorAll('b')].some((b) => b.textContent === EVIL), prev().innerHTML);
  check('anteprima: #canale diventa menzione senza # doppio', [...prev().querySelectorAll('.mention')].some((m) => m.textContent === '#generale') && !prev().textContent.includes('##'), prev().textContent);
  p.doc.getElementById('f-welcome_message').value = '';
  p.doc.getElementById('f-welcome_message').dispatchEvent(new p.win.Event('input'));
  check('anteprima: vuoto = messaggio predefinito', prev().textContent.includes('Please verify yourself'), prev().textContent);
  check('valuta: due campi di testo con valori predefiniti', p.doc.getElementById('f-currency_name').placeholder === 'ProgrammiCoin' && p.doc.getElementById('f-currency_symbol').placeholder === '💎');

  // ---- 5. moduli
  await go(p, `#/g/${fx.GA}/modules`);
  const boxes = p.doc.querySelectorAll('.mod input[type=checkbox]');
  check('moduli: 13 interruttori', boxes.length === 13, boxes.length);
  const ai = p.doc.getElementById('m-ai_chat');
  check('moduli: ai_chat bloccato e spento', ai.disabled && !ai.checked);
  check('moduli: avviso proprietario', text(p.doc).includes('Disattivato dal proprietario del bot'));
  p.doc.getElementById('m-leveling').checked = false;
  [...p.doc.querySelectorAll('button')].find((b) => b.textContent === 'Salva moduli').click();
  await sleep(80);
  const last = state.saved[state.saved.length - 1];
  check('moduli: payload con leveling=false e senza i bloccati', last.modules.leveling === false && !('ai_chat' in last.modules), JSON.stringify(last));

  // ---- 5b. ruoli
  await go(p, `#/g/${fx.GA}/roles`);
  check('ruoli: elenco dei menu esistenti', text(p.doc).includes(fx.roles.menus[0].title) && text(p.doc).includes('Scelta multipla'), text(p.doc).slice(0, 300));
  check('ruoli: solo ruoli assegnabili nel modulo di creazione', p.doc.querySelectorAll('#rm-roles input[type=checkbox]').length === fx.roles.roles.filter((r) => r.assignable).length);
  const before = state.saved.length;
  [...p.doc.querySelectorAll('button')].find((b) => b.textContent === 'Pubblica il menu').click();
  await sleep(40);
  check('ruoli: senza dati non invia nulla', state.saved.length === before && p.doc.getElementById('toast').textContent.includes('titolo'), p.doc.getElementById('toast').textContent);
  p.doc.getElementById('rm-title').value = 'Giochi';
  p.doc.getElementById('rm-channel').value = fx.roles.channels[0].id;
  p.doc.querySelector('#rm-roles input[type=checkbox]').checked = true;
  p.doc.getElementById('rm-exclusive').checked = true;
  [...p.doc.querySelectorAll('button')].find((b) => b.textContent === 'Pubblica il menu').click();
  await sleep(120);
  const created = state.saved.map((x) => x.menu).filter(Boolean)[0] || {};
  check('ruoli: POST con titolo, canale, ruoli ed esclusivo', created.title === 'Giochi' && created.channel_id === fx.roles.channels[0].id && created.role_ids.length === 1 && created.exclusive === true, JSON.stringify(created));
  check('ruoli: la POST porta il CSRF', p.calls.some((c) => c.method === 'POST' && c.path.endsWith('/role-menus') && c.headers['X-CSRF-Token'] === fx.me.csrf));
  await go(p, `#/g/${fx.GA}/roles`);
  [...p.doc.querySelectorAll('button')].find((b) => b.textContent === 'Elimina').click();
  await sleep(120);
  check('ruoli: eliminazione con DELETE', state.saved.some((x) => x.deleted && x.deleted.endsWith('/' + fx.roles.menus[0].menu_id)));

  // ---- 5c. ticket
  await go(p, `#/g/${fx.GA}/tickets`);
  check('ticket: avviso se la categoria non è configurata', text(p.doc).includes('Da configurare'));
  check('ticket: elenco con stato', text(p.doc).includes('Aperto') && text(p.doc).includes('Chiuso'));
  p.doc.getElementById('tk-channel').value = fx.tickets.channels[0].id;
  [...p.doc.querySelectorAll('button')].find((b) => b.textContent === 'Pubblica il pannello').click();
  await sleep(120);
  check('ticket: pannello pubblicato nel canale scelto', state.saved.some((x) => x.panel && x.panel.channel_id === fx.tickets.channels[0].id));
  const closeBtns = [...p.doc.querySelectorAll('button')].filter((b) => b.textContent === 'Chiudi');
  check('ticket: pulsante Chiudi solo sui ticket aperti', closeBtns.length === fx.tickets.tickets.filter((t) => t.status === 'open').length);
  closeBtns[0].click();
  await sleep(120);
  check('ticket: chiusura con POST', state.saved.some((x) => x.closed && x.closed.includes('/tickets/') && x.closed.endsWith('/close')));
  check('ticket: nessun errore JS', p.errors.length === 0, p.errors.join('|'));

  // ---- 6. rotte non valide e admin per non-owner
  await go(p, '#/g/abc');
  check('rotta con id non valido -> elenco server', text(p.doc).includes('I tuoi server'));
  await go(p, '#/admin');
  check('admin da non-owner -> torna ai server', location_ok(p), p.win.location.hash);
  function location_ok(pg) { return text(pg.doc).includes('I tuoi server') || pg.win.location.hash === '#/'; }

  // ---- 7. logout
  [...p.doc.querySelectorAll('#topRight button')].find((b) => b.textContent === 'Esci').click();
  await sleep(80);
  check('logout: POST /auth/logout con CSRF', p.calls.some((c) => c.path === '/auth/logout' && c.method === 'POST' && c.headers['X-CSRF-Token'] === fx.me.csrf));
  check('logout: torna al login', text(p.doc).includes('Accedi con Discord'));
  check('utente normale: nessun errore JS', p.errors.length === 0, p.errors.join('|'));

  // ---- 8. proprietario
  const left = [];
  const ownerHandler = (path, init) => {
    const m = init.method || 'GET';
    if (path === '/api/me') return { status: 200, body: fx.me_owner };
    if (path === '/api/admin/overview') return { status: 200, body: fx.admin };
    if (path.endsWith('/leave') && m === 'POST') { left.push(path); return { status: 200, body: { ok: true } }; }
    return null;
  };
  const o = await openPage('https://testserver/dashboard#/admin', ownerHandler);
  await sleep(80);
  const ot = text(o.doc);
  check('admin: pulsante Admin nella barra', o.doc.getElementById('topRight').textContent.includes('Admin'));
  check('admin: contatori e sistema', ot.includes('Server') && ot.includes('Database connesso') && ot.includes('discord.py'), ot.slice(0, 200));
  check('admin: modulo globale spento segnalato', ot.includes('Chat AI (spento)'));
  const rows = o.doc.querySelectorAll('tbody tr');
  check('admin: una riga per server', rows.length === fx.admin.guilds.length, rows.length);
  const leaveBtn = [...o.doc.querySelectorAll('button')].find((b) => b.textContent === 'Esci dal server');
  leaveBtn.click();
  await sleep(120);
  check('admin: uscita dal server -> POST con CSRF', left.length === 1 && left[0].startsWith('/api/admin/guilds/') && o.calls.find((c) => c.path.endsWith('/leave')).headers['X-CSRF-Token'] === fx.me_owner.csrf, left.join());
  check('admin: nessun errore JS', o.errors.length === 0, o.errors.join('|'));

  console.log('\nFALLITI:', fails);
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error('ERRORE TEST', e); process.exit(2); });
