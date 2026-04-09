const { chromium } = require('playwright');
const fs = require('fs');

const RESULTS = JSON.parse(fs.readFileSync('all_results.json', 'utf8'));

// Sportsplits.com URLs (cleanest split data)
const SPORTSPLITS_RACES = {
  'Skerries Triathlon - Triathlon National Series|18th Aug 2024': 'https://www.sportsplits.com/races/skerries-triathlon-2024-2024/events/1',
  'Tri795 Triathlon - Triathlon National Series|26th May 2024': 'https://www.sportsplits.com/races/tri795-2024/events/1',
  'TriAthy Standard Triathlon - Triathlon National Series|31st May 2025': 'https://www.sportsplits.com/races/triathy-2025-2025',
  'Dublin City Standard Triathlon - Triathlon National Series|23rd Aug 2025': 'https://www.sportsplits.com/races/rdj-dublin-city-triathlon-2025',
  'Pikeman Irish National Standard Distance Triathlon Championships|15th Sep 2024': 'https://www.sportsplits.com/races/pikeman-triathlon-2024',
};

// Raceresult.com URLs
const RACERESULT_RACES = {
  'Pulse Port Beach - Triathlon National Series|20th Sep 2025': 'https://my.raceresult.com/348909/',
  'Pulse Port Beach - Irish National Series|28th Sep 2024': 'https://my.raceresult.com/310758/',
  'Lough Ree Monster Triathlon|6th Sep 2025': 'https://my.raceresult.com/348909/',
};

// Other timing sites
const OTHER_RACES = {
  'Dublin City Sprint Triathlon - Triathlon National Series|24th Aug 2024': 'https://sportmaniacs.com/en/races/rdj-dublin-city-triathlon-2024',
  'TriAthy Standard Triathlon - Triathlon National Series|1st Jun 2024': 'https://www.sportstiming.ie/events/triathy-2024',
  'Loughrea Triathlon - Triathlon National Series|3rd Aug 2025': 'https://coretiming.ie/events/loughrea-triathlon-2025/',
};

async function setupBrowser() {
  const proxyUrl = process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
  let proxyConfig;
  if (proxyUrl) {
    const url = new URL(proxyUrl);
    proxyConfig = {
      server: `http://${url.hostname}:${url.port}`,
      username: url.username,
      password: url.password
    };
  }
  const browser = await chromium.launch({ headless: true, proxy: proxyConfig });
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  return { browser, context };
}

function normalizeAthleteName(name) {
  return name.toUpperCase()
    .replace(/MC /g, 'MC')
    .replace(/Ú/g, 'U').replace(/Á/g, 'A').replace(/Í/g, 'I')
    .replace(/É/g, 'E').replace(/Ó/g, 'O');
}

// Parse splits from a sportsplits.com table row
// Table format: Pos  Name (#bib)  Gun Time  Category (Pos)  Gender (Pos)  Swim  T1  Cycle  T2  Run
function parseSportsplitsRow(line, athleteName) {
  const normName = normalizeAthleteName(athleteName);
  const lastName = normName.split(' ').pop();
  const firstName = normName.split(' ')[0];
  const lineUpper = line.toUpperCase().replace(/MC /g, 'MC');

  if (!lineUpper.includes(lastName)) return null;
  if (!lineUpper.includes(firstName)) return null;

  // Extract all time-like patterns (HH:MM:SS or MM:SS)
  const times = line.match(/\d{2}:\d{2}:\d{2}/g);
  if (!times || times.length < 5) {
    // Maybe the table uses tab-separated values
    const parts = line.split(/\t/);
    if (parts.length >= 10) {
      return {
        swim: parts[5]?.trim() || '',
        t1: parts[6]?.trim() || '',
        cycle: parts[7]?.trim() || '',
        t2: parts[8]?.trim() || '',
        run: parts[9]?.trim() || ''
      };
    }
    return null;
  }

  // times[0] = gun time, times[1] = swim, times[2] = T1, times[3] = cycle, times[4] = T2, times[5] = run
  return {
    swim: times[1] || '',
    t1: times[2] || '',
    cycle: times[3] || '',
    t2: times[4] || '',
    run: times[5] || ''
  };
}

async function scrapeSportsplits(page, eventUrl, athleteName, overallPos, distance) {
  // For standard distance events, need to find correct sub-event first
  let url = eventUrl;

  if (!url.includes('/events/') && distance === 'Standard') {
    // Need to discover the event URLs
    console.log(`    Discovering events at ${url}...`);
    await page.goto(url, { timeout: 60000, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(4000);
    const eventLinks = await page.$$eval('a', els => els.filter(e =>
      e.href.includes('/events/') && !e.href.includes('/results/')
    ).map(e => ({ text: e.textContent.trim(), href: e.href })));
    console.log(`    Events: ${eventLinks.map(l => l.text).join(', ')}`);

    const stdEvent = eventLinks.find(l =>
      /standard|olympic/i.test(l.text)
    );
    if (stdEvent) {
      url = stdEvent.href;
    } else if (eventLinks.length > 0) {
      url = eventLinks[0].href;
    }
  } else if (!url.includes('/events/')) {
    url = `${url}/events/1`;
  }

  // Paginate through results looking for athlete
  for (let pageNum = 1; pageNum <= 20; pageNum++) {
    const pageUrl = pageNum === 1 ? url : `${url}?page=${pageNum}`;
    console.log(`    Checking page ${pageNum}...`);

    try {
      await page.goto(pageUrl, { timeout: 30000, waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(3000);
    } catch (e) {
      console.log(`    Page load failed: ${e.message.substring(0, 100)}`);
      break;
    }

    const text = await page.evaluate(() => document.body.innerText);

    // Check if athlete is on this page
    const normName = normalizeAthleteName(athleteName);
    const lastName = normName.split(' ').pop();

    if (!text.toUpperCase().replace(/MC /g, 'MC').includes(lastName)) {
      // Check if this is the last page
      if (text.includes('Page not found') || text.length < 500) break;
      continue;
    }

    // Found the athlete's page - parse their row
    const lines = text.split('\n');
    for (const line of lines) {
      const splits = parseSportsplitsRow(line, athleteName);
      if (splits) return splits;
    }

    // Also try tab-separated approach
    for (const line of lines) {
      const upper = line.toUpperCase().replace(/MC /g, 'MC');
      if (upper.includes(lastName) && upper.includes(normName.split(' ')[0])) {
        console.log(`    Found row: ${line.substring(0, 200)}`);
        // Try to extract times
        const times = line.match(/\d{2}:\d{2}:\d{2}/g);
        if (times && times.length >= 6) {
          return { swim: times[1], t1: times[2], cycle: times[3], t2: times[4], run: times[5] };
        }
      }
    }
  }
  return null;
}

async function scrapeRaceresult(page, raceUrl, athleteName) {
  console.log(`    Fetching ${raceUrl}...`);
  await page.goto(raceUrl, { timeout: 60000, waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);

  // Dismiss cookie consent
  try {
    const rejectBtn = await page.$('button:has-text("Reject all")');
    if (rejectBtn) {
      await rejectBtn.click();
      await page.waitForTimeout(1000);
    }
  } catch (e) {}
  try {
    const acceptBtn = await page.$('button:has-text("Accept")');
    if (acceptBtn) {
      await acceptBtn.click();
      await page.waitForTimeout(1000);
    }
  } catch (e) {}

  await page.waitForTimeout(3000);
  let bodyText = await page.evaluate(() => document.body.innerText);
  console.log(`    Page loaded (${bodyText.length} chars)`);

  // Try to find search input and search for athlete
  const lastName = athleteName.split(' ').pop();
  const inputs = await page.$$('input[type="text"], input[type="search"]');
  for (const input of inputs) {
    try {
      const placeholder = await input.getAttribute('placeholder');
      console.log(`    Input found: placeholder="${placeholder}"`);
      await input.click();
      await input.fill(lastName);
      await page.waitForTimeout(1000);
      await page.keyboard.press('Enter');
      await page.waitForTimeout(3000);
      break;
    } catch (e) {
      continue;
    }
  }

  bodyText = await page.evaluate(() => document.body.innerText);
  const firstName = athleteName.split(' ')[0].toUpperCase();
  const lastUpper = lastName.toUpperCase();
  const altLast = athleteName.replace(/Mc /i, 'Mc').split(' ').pop().toUpperCase();

  // Search for athlete in text
  const lines = bodyText.split('\n');
  for (const line of lines) {
    const upper = line.toUpperCase();
    if ((upper.includes(lastUpper) || upper.includes(altLast)) && upper.includes(firstName)) {
      console.log(`    Match: ${line.substring(0, 250)}`);
      // raceresult.com often uses tab/space separated values with times like HH:MM:SS
      const times = line.match(/\d{1,2}:\d{2}:\d{2}/g);
      if (times && times.length >= 4) {
        console.log(`    Times found: ${times.join(', ')}`);
        // Typical raceresult format varies; try common patterns
        // Often: Finish, Swim, T1, Bike, T2, Run or similar
        if (times.length >= 6) {
          return { swim: times[1], t1: times[2], cycle: times[3], t2: times[4], run: times[5] };
        }
      }
    }
  }

  // Try screenshot for debugging
  await page.screenshot({ path: `rr_${lastName}.png`, fullPage: false });
  console.log(`    Screenshot saved for debugging`);

  return null;
}

async function scrapeOther(page, siteUrl, athleteName) {
  console.log(`    Fetching ${siteUrl}...`);
  try {
    await page.goto(siteUrl, { timeout: 30000, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5000);
  } catch (e) {
    console.log(`    Failed: ${e.message.substring(0, 100)}`);
    return null;
  }

  const bodyText = await page.evaluate(() => document.body.innerText);
  const lastName = athleteName.split(' ').pop().toUpperCase();

  if (bodyText.toUpperCase().includes(lastName)) {
    console.log(`    Found ${lastName} in page`);
    const lines = bodyText.split('\n');
    for (const line of lines) {
      if (line.toUpperCase().includes(lastName)) {
        console.log(`    Row: ${line.substring(0, 250)}`);
        const times = line.match(/\d{1,2}:\d{2}:\d{2}/g);
        if (times && times.length >= 4) {
          console.log(`    Times: ${times.join(', ')}`);
        }
      }
    }
  } else {
    // Try clicking "Results" link
    try {
      const resultsLink = await page.$('a:has-text("Results"), a:has-text("results")');
      if (resultsLink) {
        await resultsLink.click();
        await page.waitForTimeout(5000);
        const resultsText = await page.evaluate(() => document.body.innerText);
        if (resultsText.toUpperCase().includes(lastName)) {
          console.log(`    Found ${lastName} in results page`);
          const lines = resultsText.split('\n');
          for (const line of lines) {
            if (line.toUpperCase().includes(lastName)) {
              console.log(`    Row: ${line.substring(0, 250)}`);
            }
          }
        }
      }
    } catch (e) {}
  }

  return null;
}

(async () => {
  const { browser, context } = await setupBrowser();
  const page = await context.newPage();

  const splitsData = [];

  for (const result of RESULTS) {
    const raceKey = `${result.race_name}|${result.race_date}`;
    console.log(`\n--- ${result.athlete_name}: ${result.race_name} (${result.race_date}) ---`);

    let splits = null;

    // 1. Try sportsplits.com first
    if (SPORTSPLITS_RACES[raceKey]) {
      console.log(`  Source: sportsplits.com`);
      splits = await scrapeSportsplits(page, SPORTSPLITS_RACES[raceKey], result.athlete_name, result.overall_position, result.distance);
    }

    // 2. Try raceresult.com
    if (!splits && RACERESULT_RACES[raceKey]) {
      console.log(`  Source: raceresult.com`);
      splits = await scrapeRaceresult(page, RACERESULT_RACES[raceKey], result.athlete_name);
    }

    // 3. Try other sites
    if (!splits && OTHER_RACES[raceKey]) {
      console.log(`  Source: other`);
      splits = await scrapeOther(page, OTHER_RACES[raceKey], result.athlete_name);
    }

    // 4. For races with no configured URL, note it
    if (!splits && !SPORTSPLITS_RACES[raceKey] && !RACERESULT_RACES[raceKey] && !OTHER_RACES[raceKey]) {
      console.log(`  No result URL configured for this race`);
    }

    splitsData.push({
      ...result,
      swim: splits?.swim || '',
      t1: splits?.t1 || '',
      cycle: splits?.cycle || '',
      t2: splits?.t2 || '',
      run: splits?.run || '',
    });

    if (splits) {
      console.log(`  SPLITS: Swim=${splits.swim} T1=${splits.t1} Cycle=${splits.cycle} T2=${splits.t2} Run=${splits.run}`);
    } else {
      console.log(`  NO SPLITS FOUND`);
    }
  }

  await browser.close();

  // Write CSV with splits
  const csvHeader = 'athlete_name,race_name,race_date,distance,finish_time,overall_position,age_group,score,swim,t1,cycle,t2,run';
  const csvRows = splitsData.map(r =>
    `"${r.athlete_name}","${r.race_name}","${r.race_date}","${r.distance}","${r.finish_time}","${r.overall_position}","${r.age_group}","${r.score}","${r.swim}","${r.t1}","${r.cycle}","${r.t2}","${r.run}"`
  );
  fs.writeFileSync('triathlon_results.csv', [csvHeader, ...csvRows].join('\n'));
  fs.writeFileSync('all_results.json', JSON.stringify(splitsData, null, 2));

  // Print summary
  console.log('\n\n=== SPLIT DATA SUMMARY ===');
  let found = 0, missing = 0;
  for (const r of splitsData) {
    if (r.swim) {
      found++;
      console.log(`  ${r.athlete_name} | ${r.race_name} | Swim: ${r.swim} | Cycle: ${r.cycle} | Run: ${r.run}`);
    } else {
      missing++;
      console.log(`  ${r.athlete_name} | ${r.race_name} | NO SPLITS`);
    }
  }
  console.log(`\nFound splits: ${found}/${splitsData.length}  Missing: ${missing}`);
})();
