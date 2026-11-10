(() => {
  const page = document.querySelector('.users-page');
  if (!page) return;
  const list = document.getElementById('users-list');
  const rows = [...list.querySelectorAll('.user-entry')];
  const search = document.getElementById('users-search');
  const sort = document.getElementById('users-sort');
  const direction = document.getElementById('users-direction');
  let descending = false;
  const collator = new Intl.Collator('ru', {numeric: true, sensitivity: 'base'});
  function update() {
    const query = search.value.trim().toLocaleLowerCase();
    const key = sort.value;
    rows.sort((a, b) => {
      let result;
      if (key === 'activity') {
        const av = Number(a.dataset.activity), bv = Number(b.dataset.activity);
        if (!av || !bv) return av === bv ? collator.compare(a.dataset.name, b.dataset.name) : (av ? -1 : 1);
        result = av - bv;
      } else if (key === 'state') result = Number(a.dataset.state) - Number(b.dataset.state);
      else result = collator.compare(a.dataset[key], b.dataset[key]);
      return (descending ? -result : result) || collator.compare(a.dataset.name, b.dataset.name);
    });
    let visible = 0;
    for (const row of rows) {
      row.hidden = !`${row.dataset.name} ${row.dataset.email}`.toLocaleLowerCase().includes(query);
      if (!row.hidden) visible++;
      list.append(row);
    }
    document.getElementById('users-count').textContent = `Показано: ${visible} из ${rows.length}`;
    document.getElementById('users-empty').hidden = visible !== 0;
    list.hidden = visible === 0;
    direction.textContent = descending ? '↓' : '↑';
    direction.title = descending ? 'По убыванию' : 'По возрастанию';
  }
  search.addEventListener('input', update);
  sort.addEventListener('change', update);
  direction.addEventListener('click', () => { descending = !descending; update(); });
  const add = document.getElementById('add-user');
  const create = document.getElementById('create-user');
  add.hidden = false;
  page.classList.add('enhanced');
  add.addEventListener('click', () => {
    create.open = true;
    create.querySelector('input[name="username"]').focus();
  });
  update();
})();