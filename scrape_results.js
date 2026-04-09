const { chromium } = require('playwright');
const fs = require('fs');

const ATHLETES = [
  'Matthew Carroll',
  'Darragh Ryan',
  'Andy Mc Namara',
  'Patrick Scallan',
  'Daniel Lyons',
  'John Harney',
  'Ross Murphy',
  'Ciarán Reynolds',
  'Arron Murphy',
  'Philip Sheridan',
  'Declan Morley',
  'Daniel Eckersall',
  'Thomas Landy',
  'Brian Smyth',
  'Jojo Salmon',
];

// Also try alternate name spellings
const ALTERNATE_NAMES = {
  'Andy Mc Namara': ['Andy McNamara', 'Andrew Mc Namara', 'Andrew McNamara'],
  'Ciarán Reynolds': ['Ciaran Reynolds'],
  'Jojo Salmon': ['JoJo Salmon', 'Joseph Salmon', 'Jo Jo Salmon'],
  'Arron Murphy': ['Aaron Murphy'],
  'Brian Smyth': ['Brian Smith'],
};

async function setupBrowser() {
  const proxyUrl = process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
  let proxyConfig = undefined;
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

async function searchAthlete(page, athleteName) {
  console.log(`  Searching for "${athleteName}"...`);
  await page.goto('https://www.mytriranking.com/triathlonireland/findathlete', {
    timeout: 60000, waitUntil: 'domcontentloaded'
  });
  await page.waitForTimeout(6000);

  const searchInput = await page.$('input[placeholder*="athlete name"]');
  if (!searchInput) {
    console.log('    No search input found');
    return [];
  }

  await searchInput.click();
  await page.keyboard.type(athleteName, { delay: 60 });
  await page.waitForTimeout(4000);

  // Get all athlete links and their table row data
  const results = await page.$$eval('tr.UhXTve', rows => {
    return rows.map(row => {
      const cells = row.querySelectorAll('td');
      const link = row.querySelector('a[href*="/irishathlete/"]');
      return {
        name: cells[0]?.textContent?.trim() || '',
        discipline: cells[1]?.textContent?.trim() || '',
        raceCategory: cells[2]?.textContent?.trim() || '',
        irishRank: cells[3]?.textContent?.trim() || '',
        ageGroup: cells[4]?.textContent?.trim() || '',
        agRank: cells[5]?.textContent?.trim() || '',
        score: cells[6]?.textContent?.trim() || '',
        athleteId: cells[7]?.textContent?.trim() || '',
        profileUrl: link?.href || ''
      };
    });
  });

  return results;
}

async function scrapeAthleteProfile(page, athleteId, athleteName) {
  console.log(`  Visiting profile ${athleteId} for ${athleteName}...`);
  await page.goto(`https://www.mytriranking.com/irishathlete/${athleteId}`, {
    timeout: 60000, waitUntil: 'domcontentloaded'
  });
  await page.waitForTimeout(8000);

  // First check if there's a "Discipline: All" filter and select it
  // Also check if there's a "Distance: All" filter
  const bodyText = await page.evaluate(() => document.body.innerText);

  // Try to click "All" in discipline filter to see all disciplines
  const allButtons = await page.$$('button, [role="option"], [role="tab"]');
  for (const btn of allButtons) {
    const text = await btn.textContent();
    if (text.trim() === 'All' || text.trim() === 'all') {
      try {
        await btn.click();
        await page.waitForTimeout(2000);
      } catch(e) {}
    }
  }

  // Also try clicking dropdown options with "All"
  const selectOptions = await page.$$('[data-hook*="dropdown"] option, select option');
  for (const opt of selectOptions) {
    const text = await opt.textContent();
    if (text.trim() === 'All') {
      try {
        await opt.click();
        await page.waitForTimeout(1000);
      } catch(e) {}
    }
  }

  await page.waitForTimeout(2000);

  // Extract all race results from the table
  // The profile has a results table with rows containing: Date, Race Name, Discipline, Distance, Score, Position, Age Group, Finish Time, Projected World #1 Time
  const races = await page.$$eval('tr.UhXTve', rows => {
    return rows.map(row => {
      const cells = row.querySelectorAll('td');
      if (cells.length < 7) return null;
      return {
        date: cells[0]?.textContent?.trim() || '',
        raceName: cells[1]?.textContent?.trim() || '',
        discipline: cells[2]?.textContent?.trim() || '',
        distance: cells[3]?.textContent?.trim() || '',
        score: cells[4]?.textContent?.trim() || '',
        position: cells[5]?.textContent?.trim() || '',
        ageGroup: cells[6]?.textContent?.trim() || '',
        finishTime: cells[7]?.textContent?.trim() || '',
        projectedTime: cells[8]?.textContent?.trim() || ''
      };
    }).filter(r => r !== null);
  });

  // Check for pagination (Next button)
  let allRaces = [...races];
  let hasNext = true;
  let pageNum = 1;

  while (hasNext && pageNum < 20) {
    const nextBtn = await page.$('button[aria-label="Next"]');
    if (!nextBtn) { hasNext = false; break; }

    const isDisabled = await nextBtn.evaluate(el =>
      el.disabled || el.getAttribute('aria-disabled') === 'true'
    );
    if (isDisabled) { hasNext = false; break; }

    await nextBtn.click();
    await page.waitForTimeout(3000);
    pageNum++;

    const moreRaces = await page.$$eval('tr.UhXTve', rows => {
      return rows.map(row => {
        const cells = row.querySelectorAll('td');
        if (cells.length < 7) return null;
        return {
          date: cells[0]?.textContent?.trim() || '',
          raceName: cells[1]?.textContent?.trim() || '',
          discipline: cells[2]?.textContent?.trim() || '',
          distance: cells[3]?.textContent?.trim() || '',
          score: cells[4]?.textContent?.trim() || '',
          position: cells[5]?.textContent?.trim() || '',
          ageGroup: cells[6]?.textContent?.trim() || '',
          finishTime: cells[7]?.textContent?.trim() || '',
          projectedTime: cells[8]?.textContent?.trim() || ''
        };
      }).filter(r => r !== null);
    });

    allRaces = [...allRaces, ...moreRaces];
    console.log(`    Page ${pageNum}: found ${moreRaces.length} more races`);
  }

  return allRaces;
}

function parseDate(dateStr) {
  // Format: "6th Sep 2025" or "15th Mar 2024"
  const cleaned = dateStr.replace(/(st|nd|rd|th)/g, '');
  return new Date(cleaned);
}

function filterByYear(races, years) {
  return races.filter(r => {
    const d = parseDate(r.date);
    return years.includes(d.getFullYear());
  });
}

function timeToSeconds(timeStr) {
  if (!timeStr) return Infinity;
  const parts = timeStr.split(':');
  if (parts.length === 3) {
    return parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseInt(parts[2]);
  }
  if (parts.length === 2) {
    return parseInt(parts[0]) * 60 + parseInt(parts[1]);
  }
  return Infinity;
}

(async () => {
  const { browser, context } = await setupBrowser();
  const page = await context.newPage();

  const allResults = [];
  const athleteNotFound = [];

  for (const athlete of ATHLETES) {
    console.log(`\n=== Processing: ${athlete} ===`);

    // Search for athlete
    let searchResults = await searchAthlete(page, athlete);

    // Filter to Triathlon discipline and matching age groups (M30-34, M35-39)
    let triathlonResults = searchResults.filter(r =>
      r.discipline === 'Triathlon' &&
      r.name.toUpperCase().includes(athlete.split(' ').pop().toUpperCase())
    );

    // If no results, try alternate names
    if (triathlonResults.length === 0 && ALTERNATE_NAMES[athlete]) {
      for (const altName of ALTERNATE_NAMES[athlete]) {
        console.log(`  Trying alternate name: ${altName}`);
        searchResults = await searchAthlete(page, altName);
        triathlonResults = searchResults.filter(r =>
          r.discipline === 'Triathlon'
        );
        if (triathlonResults.length > 0) break;
      }
    }

    if (triathlonResults.length === 0) {
      console.log(`  *** No triathlon results found for ${athlete} ***`);
      // Also try just the last name
      const lastName = athlete.split(' ').pop();
      if (lastName !== athlete) {
        console.log(`  Trying last name only: ${lastName}`);
        searchResults = await searchAthlete(page, lastName);
        // Look for first name match
        const firstName = athlete.split(' ')[0].toUpperCase();
        triathlonResults = searchResults.filter(r =>
          r.discipline === 'Triathlon' &&
          r.name.toUpperCase().includes(firstName) &&
          (r.ageGroup === '30-34' || r.ageGroup === '35-39')
        );
      }
    }

    if (triathlonResults.length === 0) {
      console.log(`  !!! ATHLETE NOT FOUND: ${athlete}`);
      athleteNotFound.push(athlete);
      continue;
    }

    // For each matching profile (could be 1 or more), scrape results
    for (const match of triathlonResults) {
      console.log(`  Found: ${match.name} (ID: ${match.athleteId}, AG: ${match.ageGroup})`);
      const races = await scrapeAthleteProfile(page, match.athleteId, athlete);
      console.log(`  Total races on profile: ${races.length}`);

      // Filter for 2024 and 2025, triathlon only (exclude duathlons)
      const filtered = filterByYear(races, [2024, 2025]).filter(r =>
        r.discipline === 'Triathlon'
      );
      console.log(`  Triathlon races in 2024-2025: ${filtered.length}`);

      for (const race of filtered) {
        allResults.push({
          athlete_name: athlete,
          race_name: race.raceName,
          race_date: race.date,
          distance: race.distance,
          finish_time: race.finishTime,
          overall_position: race.position,
          ag_position: race.ageGroup,
          score: race.score
        });
      }
    }
  }

  await browser.close();

  // Write CSV
  console.log('\n\n=== GENERATING CSV ===');
  const csvHeader = 'athlete_name,race_name,race_date,distance,finish_time,overall_position,ag_position,score';
  const csvRows = allResults.map(r =>
    `"${r.athlete_name}","${r.race_name}","${r.race_date}","${r.distance}","${r.finish_time}","${r.overall_position}","${r.ag_position}","${r.score}"`
  );
  const csv = [csvHeader, ...csvRows].join('\n');
  fs.writeFileSync('triathlon_results.csv', csv);
  console.log(`CSV written: ${allResults.length} results to triathlon_results.csv`);

  // Generate summary: best sprint triathlon finish time per athlete
  console.log('\n=== SPRINT TRIATHLON RANKING (Best Finish Time) ===');
  const sprintResults = allResults.filter(r =>
    r.distance.toLowerCase().includes('sprint')
  );

  const bestByAthlete = {};
  for (const r of sprintResults) {
    const secs = timeToSeconds(r.finish_time);
    if (!bestByAthlete[r.athlete_name] || secs < bestByAthlete[r.athlete_name].seconds) {
      bestByAthlete[r.athlete_name] = {
        seconds: secs,
        finish_time: r.finish_time,
        race_name: r.race_name,
        race_date: r.race_date,
        score: r.score
      };
    }
  }

  const ranked = Object.entries(bestByAthlete)
    .sort((a, b) => a[1].seconds - b[1].seconds)
    .map(([name, data], i) => ({
      rank: i + 1,
      athlete: name,
      best_sprint_time: data.finish_time,
      race: data.race_name,
      date: data.race_date,
      score: data.score
    }));

  // Print summary table
  console.log('Rank | Athlete | Best Sprint Time | Race | Date | Score');
  console.log('-----|---------|-----------------|------|------|------');
  for (const r of ranked) {
    console.log(`${r.rank} | ${r.athlete} | ${r.best_sprint_time} | ${r.race} | ${r.date} | ${r.score}`);
  }

  // Save summary as CSV too
  const summaryHeader = 'rank,athlete,best_sprint_time,race,date,score';
  const summaryRows = ranked.map(r =>
    `${r.rank},"${r.athlete}","${r.best_sprint_time}","${r.race}","${r.date}","${r.score}"`
  );
  const summaryCsv = [summaryHeader, ...summaryRows].join('\n');
  fs.writeFileSync('sprint_ranking_summary.csv', summaryCsv);
  console.log(`\nSummary written to sprint_ranking_summary.csv`);

  // Report athletes not found
  if (athleteNotFound.length > 0) {
    console.log(`\n=== ATHLETES NOT FOUND ON MYTRIRANKING ===`);
    for (const name of athleteNotFound) {
      console.log(`  - ${name}`);
    }
  }

  // Write raw data as JSON for inspection
  fs.writeFileSync('all_results.json', JSON.stringify(allResults, null, 2));
  console.log('\nRaw JSON data written to all_results.json');
})();
