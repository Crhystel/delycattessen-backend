// k6 stress test of the cashier purchase flow, with parents and teachers using the app (SCRUM-26).
//   k6 run --out web-dashboard=export=pruebas-carga/resultados/reporte-estres.html pruebas-carga/pos_compra.js
//
// A "purchase" is what a cashier does at the POS: identify the customer by QR and
// charge the sale (a student pays with the wallet, a teacher on credit). While the
// cashiers work, parents and teachers use the app in parallel scenarios.
// Settings come from the LOAD_* lines of the backend .env (or `-e LOAD_X=...`);
// see pruebas-carga/README.md.
import http from 'k6/http';
import exec from 'k6/execution';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

function readEnvFile() {
  try {
    return open('../.env');
  } catch (error) {
    return '';
  }
}

// Only LOAD_* lines are read; the rest of the .env is ignored.
function parseLoadVars(text) {
  const vars = {};
  text.split(/\r?\n/).forEach((line) => {
    const match = line.match(/^\s*(LOAD_[A-Z_]+)\s*=\s*(.*?)\s*$/);
    if (match) {
      vars[match[1]] = match[2].replace(/^["']|["']$/g, '');
    }
  });
  return vars;
}

const fileVars = parseLoadVars(readEnvFile());
const setting = (name, fallback) => __ENV[name] || fileVars[name] || fallback;

const BASE_URL = setting('LOAD_BASE_URL', 'http://localhost:8000');
const PASSWORD = setting('LOAD_PASSWORD', '');
const CASHIERS = parseInt(setting('LOAD_CASHIERS', '25'), 10);
const STUDENTS = parseInt(setting('LOAD_STUDENTS', '50'), 10);
const PARENTS = parseInt(setting('LOAD_PARENTS', '25'), 10);
const TEACHERS = parseInt(setting('LOAD_TEACHERS', '12'), 10);
const STEP_SECONDS = parseInt(setting('LOAD_STEP_SECONDS', '120'), 10);
const THINK_SECONDS = parseFloat(setting('LOAD_THINK_SECONDS', '3'));
const PROFILE = setting('LOAD_PROFILE', 'carga');
// Parents and teachers online per cashier; 0 turns that role off (cashier-only baseline).
const PARENT_RATIO = parseFloat(setting('LOAD_PARENT_RATIO', '1'));
const TEACHER_RATIO = parseFloat(setting('LOAD_TEACHER_RATIO', '0.5'));
// Share of the POS purchases made by a teacher (on credit) and of parent visits that end in a pre-order.
const TEACHER_SHARE = parseFloat(setting('LOAD_TEACHER_SHARE', '0.2'));
const PREORDER_SHARE = parseFloat(setting('LOAD_PREORDER_SHARE', '0.2'));
const PARENT_THINK_SECONDS = parseFloat(setting('LOAD_PARENT_THINK_SECONDS', '6'));
const TEACHER_THINK_SECONDS = parseFloat(setting('LOAD_TEACHER_THINK_SECONDS', '10'));

// carga: the 15-25 cashiers of the acceptance criterion. estres: past the criterion, to find the limit.
const PROFILES = { carga: [15, 20, 25], estres: [50, 100, 150] };
const TARGETS = PROFILES[PROFILE];
if (!TARGETS) {
  throw new Error(`Unknown LOAD_PROFILE "${PROFILE}". Use "carga" or "estres".`);
}
const WITH_PARENTS = PARENT_RATIO > 0 && PARENTS > 0;
const WITH_TEACHERS = TEACHER_RATIO > 0 && TEACHERS > 0;

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// A purchase = identify + sale. Only completed purchases feed the duration.
const compraDuration = new Trend('compra_duration', true);
const compraCompleted = new Rate('compra_completed');
const compraUnder5s = new Rate('compra_under_5s');

// One hold per level; the `users` tag of every sample says which cashier level it belongs to.
const RAMP_SECONDS = 20;
const STAGES = [];
TARGETS.forEach((target) => {
  STAGES.push({ seconds: RAMP_SECONDS, target, label: 'ramp' });
  STAGES.push({ seconds: STEP_SECONDS, target, label: String(target) });
});
STAGES.push({ seconds: RAMP_SECONDS, target: 0, label: 'ramp' });

const scaledStages = (ratio) => STAGES.map(({ seconds, target }) => ({
  duration: `${seconds}s`, target: Math.ceil(target * ratio),
}));
const scenario = (execName, ratio) => ({
  executor: 'ramping-vus', startVUs: 0, stages: scaledStages(ratio), exec: execName, gracefulRampDown: '30s',
});

const scenarios = { cajeros: scenario('cashier', 1) };
if (WITH_PARENTS) {
  scenarios.padres = scenario('parent', PARENT_RATIO);
}
if (WITH_TEACHERS) {
  scenarios.docentes = scenario('teacher', TEACHER_RATIO);
}

const thresholds = {};
if (PROFILE === 'carga') {
  // Acceptance criterion: 95 % of the purchases finish in under 10 s.
  thresholds.compra_duration = ['p(95)<10000'];
  thresholds.compra_completed = ['rate>0.99'];
  thresholds['http_req_failed{step:identify}'] = ['rate<0.01'];
  thresholds['http_req_failed{step:sale}'] = ['rate<0.01'];
}
// Per level, so the summary shows which cashier counts hold and which do not.
TARGETS.forEach((target) => {
  thresholds[`compra_duration{users:${target}}`] = ['p(95)<10000'];
  thresholds[`compra_completed{users:${target}}`] = ['rate>0.99'];
});
// Parents and teachers: almost no errors and a screen that answers in under 5 s.
['parent', 'teacher'].forEach((role) => {
  if ((role === 'parent' && WITH_PARENTS) || (role === 'teacher' && WITH_TEACHERS)) {
    thresholds[`http_req_failed{role:${role}}`] = ['rate<0.01'];
    thresholds[`http_req_duration{role:${role}}`] = ['p(95)<5000'];
  }
});

export const options = {
  scenarios,
  // Every student and teacher logs in during setup (PBKDF2 is slow on purpose).
  setupTimeout: '300s',
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  thresholds,
};

function currentLevel() {
  const elapsed = (Date.now() - exec.scenario.startTime) / 1000;
  let end = 0;
  for (let index = 0; index < STAGES.length; index += 1) {
    end += STAGES[index].seconds;
    if (elapsed < end) {
      return STAGES[index].label;
    }
  }
  return STAGES[STAGES.length - 1].label;
}

const pad = (number) => String(number).padStart(2, '0');
const cashierLogin = (number) => `carga_cajero_${pad(number)}@carga.test`;
const studentLogin = (number) => `carga_estudiante_${pad(number)}`;
const teacherLogin = (number) => `carga_docente_${pad(number)}@carga.test`;
const parentLogin = (number) => `carga_padre_${pad(number)}@carga.test`;
const bearer = (token) => ({ ...JSON_HEADERS, Authorization: `Bearer ${token}` });
const pick = (list) => list[Math.floor(Math.random() * list.length)];
const listOf = (body) => (Array.isArray(body) ? body : (body && body.results) || []);

function login(username, step) {
  const response = http.post(
    `${BASE_URL}/api/users/login/`,
    JSON.stringify({ username, password: PASSWORD }),
    { headers: JSON_HEADERS, tags: { step } },
  );
  if (response.status !== 200) {
    throw new Error(`Login failed for ${username} (${response.status}). Did you run the seed and set LOAD_PASSWORD?`);
  }
  return response.json('access');
}

export function setup() {
  if (!PASSWORD) {
    throw new Error('LOAD_PASSWORD is not set. Add the LOAD_* lines to the backend .env.');
  }
  const students = [];
  for (let number = 1; number <= STUDENTS; number += 1) {
    students.push({ token: login(studentLogin(number), 'setup_login') });
  }
  // Teachers who also buy at the POS (on credit).
  const teachers = [];
  if (TEACHER_SHARE > 0) {
    for (let number = 1; number <= TEACHERS; number += 1) {
      teachers.push({ token: login(teacherLogin(number), 'setup_login') });
    }
  }

  // Products of the test sede, read with the first cashier.
  const cashierToken = login(cashierLogin(1), 'setup_login');
  const menu = http.get(`${BASE_URL}/api/catalog/menu/`, { headers: bearer(cashierToken), tags: { step: 'setup_menu' } });
  const products = listOf(menu.json()).map((row) => row.id);
  if (products.length === 0) {
    throw new Error('The test sede has no products. Run the seed first.');
  }
  return { students, teachers, products };
}

// Module scope is per virtual user in k6: each user logs in once.
let cashierToken = null;
let parentToken = null;
let teacherToken = null;

// Cashier: identify a customer by QR and charge the sale.
export function cashier(data) {
  if (cashierToken === null) {
    // With more users than cashier accounts, several users share an account.
    cashierToken = login(cashierLogin(((__VU - 1) % CASHIERS) + 1), 'login_cashier');
  }
  const users = currentLevel();
  const byTeacher = data.teachers.length > 0 && Math.random() < TEACHER_SHARE;
  const customer = byTeacher ? pick(data.teachers) : pick(data.students);
  const productId = pick(data.products);
  const customerType = byTeacher ? 'teacher' : 'student';

  // The customer's phone would show this QR; it is not part of the cashier's time.
  const qr = http.get(`${BASE_URL}/api/users/qr/token/`, {
    headers: bearer(customer.token), tags: { step: 'customer_qr', users, customer: customerType },
  });
  if (!check(qr, { 'customer QR issued': (response) => response.status === 200 })) {
    sleep(1);
    return;
  }
  const { token, user_id: userId } = qr.json();

  const started = Date.now();
  const identify = http.post(
    `${BASE_URL}/api/pos/identify/qr/`, JSON.stringify({ token }),
    { headers: bearer(cashierToken), tags: { step: 'identify', users, customer: customerType, role: 'cashier' } },
  );
  let completed = check(identify, { 'identify answered 200': (response) => response.status === 200 });

  if (completed) {
    const sale = http.post(
      `${BASE_URL}/api/pos/sales/`,
      JSON.stringify({
        payment_method: byTeacher ? 'CREDIT' : 'WALLET',
        user_id: userId,
        items: [{ menu_item_id: productId, quantity: 1 }],
      }),
      { headers: bearer(cashierToken), tags: { step: 'sale', users, customer: customerType, role: 'cashier' } },
    );
    completed = check(sale, { 'sale answered 201': (response) => response.status === 201 });
  }

  const elapsed = Date.now() - started;
  compraCompleted.add(completed, { users });
  if (completed) {
    compraDuration.add(elapsed, { users });
    compraUnder5s.add(elapsed < 5000, { users });
  }
  sleep(THINK_SECONDS * (0.5 + Math.random()));
}

// Parent: looks at a child (wallet, menu, orders, purchases) and sometimes pre-orders a day.
export function parent() {
  if (parentToken === null) {
    parentToken = login(parentLogin(((__VU - 1) % PARENTS) + 1), 'login_parent');
  }
  const users = currentLevel();
  const get = (path, step) => http.get(`${BASE_URL}${path}`, {
    headers: bearer(parentToken), tags: { step, users, role: 'parent' },
  });

  get('/api/users/me/', 'parent_me');
  const children = get('/api/users/children/', 'parent_children');
  if (!check(children, { 'children listed': (response) => response.status === 200 })) {
    sleep(1);
    return;
  }
  const child = pick(listOf(children.json()));
  get(`/api/wallet/${child.wallet_id}/transactions/`, 'parent_wallet');
  const menu = get(`/api/catalog/students/${child.id}/monthly_menu/`, 'parent_menu');
  get(`/api/pos/preorders/?student_id=${child.id}`, 'parent_preorders');
  get(`/api/pos/sales/history/?student_id=${child.id}`, 'parent_sales');

  if (menu.status === 200 && Math.random() < PREORDER_SHARE) {
    const days = listOf(menu.json()).filter((day) => day.dishes.length > 0 && !day.is_blocked_by_parental_control);
    if (days.length > 0) {
      const day = pick(days);
      const order = http.post(
        `${BASE_URL}/api/pos/preorder/`,
        JSON.stringify({
          student_id: child.id,
          delivery_date: day.date,
          items: [{ dish_id: pick(day.dishes).id, quantity: 1 }],
        }),
        { headers: bearer(parentToken), tags: { step: 'parent_preorder', users, role: 'parent' } },
      );
      check(order, { 'pre-order created': (response) => response.status === 201 });
    }
  }
  sleep(PARENT_THINK_SECONDS * (0.5 + Math.random()));
}

// Teacher: opens the app to see the consumption on credit and to show the QR.
export function teacher() {
  if (teacherToken === null) {
    teacherToken = login(teacherLogin(((__VU - 1) % TEACHERS) + 1), 'login_teacher');
  }
  const users = currentLevel();
  const get = (path, step) => http.get(`${BASE_URL}${path}`, {
    headers: bearer(teacherToken), tags: { step, users, role: 'teacher' },
  });

  get('/api/users/me/', 'teacher_me');
  get('/api/pos/credit/history/', 'teacher_credit');
  get('/api/users/qr/token/', 'teacher_qr');
  sleep(TEACHER_THINK_SECONDS * (0.5 + Math.random()));
}
