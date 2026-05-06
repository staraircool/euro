"""EuroPages Company Scraper - Full contact extraction with Playwright.

Scrapes company data from europages.co.uk including:
- Company name, Email, Website, Phone number
- Country, Address, Description, Company type

Uses Playwright for browser rendering to extract hidden contact info.
Uses Apify REST API directly to avoid SDK dependency conflicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import quote_plus, urljoin

import httpx

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('europages-scraper')

BASE_URL = 'https://www.europages.co.uk'


async def main() -> None:
    """Main entry point for the Apify Actor."""
    apify_token = os.environ.get('APIFY_TOKEN', '')
    dataset_id = os.environ.get('APIFY_DEFAULT_DATASET_ID', '')
    kv_store_id = os.environ.get('APIFY_DEFAULT_KEY_VALUE_STORE_ID', '')

    # Read input
    actor_input = {}
    if apify_token and kv_store_id:
        log.info('Running on Apify platform')
        async with httpx.AsyncClient() as api_client:
            try:
                resp = await api_client.get(
                    f'https://api.apify.com/v2/key-value-stores/{kv_store_id}/records/INPUT',
                    params={'token': apify_token},
                )
                if resp.status_code == 200:
                    actor_input = resp.json()
            except Exception as e:
                log.warning(f'Failed to read input: {e}')
    else:
        input_path = os.path.join('storage', 'key_value_stores', 'default', 'INPUT.json')
        if os.path.exists(input_path):
            with open(input_path) as f:
                actor_input = json.load(f)

    log.info(f'Input: {json.dumps(actor_input, indent=2)}')

    # Run the scraper
    results = await run_scraper(actor_input)

    # Push results to Apify dataset
    if apify_token and dataset_id:
        async with httpx.AsyncClient() as api_client:
            # Push all results in one batch
            if results:
                try:
                    resp = await api_client.post(
                        f'https://api.apify.com/v2/datasets/{dataset_id}/items',
                        params={'token': apify_token},
                        json=results,
                        timeout=30.0,
                    )
                    log.info(f'Pushed {len(results)} items to dataset (status: {resp.status_code})')
                except Exception as e:
                    log.error(f'Failed to push data: {e}')
    else:
        dataset_dir = os.path.join('storage', 'datasets', 'default')
        os.makedirs(dataset_dir, exist_ok=True)
        for i, item in enumerate(results):
            with open(os.path.join(dataset_dir, f'{i:06d}.json'), 'w') as f:
                json.dump(item, f, indent=2)

    log.info(f'Done! {len(results)} companies with contact info.')


async def run_scraper(actor_input: dict) -> list[dict]:
    """Run the EuroPages scraper with Playwright browser."""
    search_query = actor_input.get('searchQuery', 'construction')
    start_urls = actor_input.get('startUrls', [])
    max_results = actor_input.get('maxResults', 100)
    max_pages = actor_input.get('maxPages', 5)

    results = []

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
        )

        # Build URL list
        if start_urls:
            listing_urls = []
            for url_item in start_urls:
                url = url_item.get('url', url_item) if isinstance(url_item, dict) else str(url_item)
                listing_urls.append(url)
        else:
            listing_urls = [f'{BASE_URL}/companies/{quote_plus(search_query)}.html']

        # Step 1: Collect company URLs from listing pages
        company_urls = []
        for listing_url in listing_urls:
            if _is_company_url(listing_url):
                company_urls.append(listing_url)
            else:
                found = await scrape_listing_pages(context, listing_url, max_pages, max_results - len(company_urls))
                company_urls.extend(found)
                if 0 < max_results <= len(company_urls):
                    company_urls = company_urls[:max_results]
                    break

        log.info(f'Collected {len(company_urls)} company URLs')

        # Step 2: Scrape each company detail page with browser
        for i, url in enumerate(company_urls):
            if 0 < max_results <= len(results):
                break
            try:
                log.info(f'[{i+1}/{len(company_urls)}] Scraping: {url}')
                data = await scrape_company_page(context, url)
                if data and data.get('companyName'):
                    results.append(data)
                    log.info(
                        f'  ✓ {data["companyName"]} | '
                        f'Phone: {data.get("phoneNumber") or "—"} | '
                        f'Email: {data.get("email") or "—"} | '
                        f'Web: {data.get("website") or "—"}'
                    )
                else:
                    log.warning(f'  ✗ No data from {url}')
            except Exception as e:
                log.error(f'  ✗ Error: {e}')

        await browser.close()

    return results


async def scrape_listing_pages(context, base_url: str, max_pages: int, max_items: int) -> list[str]:
    """Use browser to scrape listing pages and collect company URLs."""
    company_urls = []
    page = await context.new_page()

    try:
        for page_num in range(1, max_pages + 1):
            if 0 < max_items <= len(company_urls):
                break

            url = base_url if page_num == 1 else f'{base_url}{"&" if "?" in base_url else "?"}page={page_num}'
            log.info(f'Listing page {page_num}: {url}')

            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                log.error(f'Failed to load listing page: {e}')
                break

            # Dismiss cookie banner
            await _dismiss_cookies(page)

            # Extract company links using JavaScript
            links = await page.evaluate('''() => {
                const urls = new Set();
                for (const a of document.querySelectorAll('a[href]')) {
                    const href = a.getAttribute('href');
                    if (href && /\\/[A-Za-z0-9][^\\/]*\\/SEAC\\d+-\\d+\\.html/.test(href)) {
                        const full = href.startsWith('http') ? href : window.location.origin + href;
                        urls.add(full);
                    }
                }
                return [...urls];
            }''')

            log.info(f'  Found {len(links)} company links')

            for link in links:
                if link not in company_urls:
                    company_urls.append(link)

            if len(links) == 0:
                log.info('  No more links, stopping pagination')
                break

    finally:
        await page.close()

    return company_urls


async def scrape_company_page(context, url: str) -> dict:
    """Scrape a single company page using browser to get full contact info."""
    page = await context.new_page()
    data = {
        'companyName': '', 'email': '', 'website': '',
        'phoneNumber': '', 'country': '', 'address': '',
        'description': '', 'companyType': '', 'europagesUrl': url,
    }

    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(2000)

        # Dismiss cookies
        await _dismiss_cookies(page)
        await page.wait_for_timeout(500)

        # Click ALL "Show number" / reveal buttons to unhide phone numbers
        await _click_reveal_buttons(page)
        await page.wait_for_timeout(2000)

        # Extract all data using comprehensive JavaScript
        extracted = await page.evaluate('''() => {
            const result = {
                companyName: '', email: '', website: '',
                phoneNumber: '', country: '', address: '',
                description: '', companyType: '',
            };

            // ── COMPANY NAME ──
            const h1 = document.querySelector('h1');
            if (h1) {
                let name = '';
                for (const node of h1.childNodes) {
                    if (node.nodeType === Node.TEXT_NODE) name += node.textContent;
                }
                if (!name) name = h1.textContent;
                result.companyName = name.replace(/Verified/gi, '').replace(/✓/g, '').trim();
            }

            // ── EMAIL ──
            const excludeDomains = ['europages', 'sentry.io', 'googleapis', 'google.com',
                'facebook.com', 'hotjar.com', 'segment.io', 'mixpanel', 'intercom',
                'cloudflare', 'example.com', 'wixpress'];

            // Check mailto links
            for (const a of document.querySelectorAll('a[href^="mailto:"]')) {
                const email = a.href.replace('mailto:', '').split('?')[0].trim();
                if (email && email.includes('@') && !excludeDomains.some(d => email.includes(d))) {
                    result.email = email;
                    break;
                }
            }

            // Fallback: scan visible text for email patterns
            if (!result.email) {
                const bodyText = document.body.innerText;
                const emailRegex = /[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}/g;
                const matches = bodyText.match(emailRegex) || [];
                for (const m of matches) {
                    if (!excludeDomains.some(d => m.toLowerCase().includes(d))) {
                        result.email = m;
                        break;
                    }
                }
            }

            // ── PHONE NUMBER ──
            // Check tel: links (including ones revealed by clicking)
            for (const a of document.querySelectorAll('a[href^="tel:"]')) {
                const phone = a.href.replace('tel:', '').trim();
                if (phone && phone.length >= 7) {
                    result.phoneNumber = phone;
                    break;
                }
            }

            // Look for phone text near "Phone" / "Tel" labels
            if (!result.phoneNumber) {
                const allText = document.body.innerText;
                const phoneMatch = allText.match(/(?:Phone|Tel|Telephone|Mobile|Fax)[:\\s]*([+\\d\\s()\\-\\.]{7,25})/i);
                if (phoneMatch) {
                    result.phoneNumber = phoneMatch[1].trim();
                }
            }

            // Look for phone numbers in elements with phone-related classes
            if (!result.phoneNumber) {
                const phoneEls = document.querySelectorAll(
                    '[class*="phone" i], [class*="tel" i], [data-testid*="phone"]'
                );
                for (const el of phoneEls) {
                    const text = el.innerText.trim();
                    const match = text.match(/[+]?[\\d\\s()\\-\\.]{7,25}/);
                    if (match) {
                        result.phoneNumber = match[0].trim();
                        break;
                    }
                }
            }

            // ── WEBSITE ──
            const skipDomains = ['europages', 'google', 'facebook', 'linkedin',
                'twitter', 'instagram', 'youtube', 'maps.google', 'apple.com'];

            for (const a of document.querySelectorAll('a[target="_blank"][href^="http"]')) {
                const href = a.href;
                if (!skipDomains.some(d => href.includes(d))) {
                    result.website = href;
                    break;
                }
            }
            if (!result.website) {
                for (const a of document.querySelectorAll('a[href^="http"]')) {
                    const text = (a.textContent || '').toLowerCase();
                    const href = a.href;
                    if ((text.includes('website') || text.includes('visit') || text.includes('www'))
                        && !skipDomains.some(d => href.includes(d))) {
                        result.website = href;
                        break;
                    }
                }
            }

            // ── COUNTRY ──
            const countryEl = document.querySelector('[class*="country" i]');
            if (countryEl) result.country = countryEl.textContent.trim();

            if (!result.country && h1) {
                const parentText = h1.parentElement ? h1.parentElement.innerText : '';
                const countries = ['Germany','France','Italy','Spain','Poland','Netherlands',
                    'Belgium','Austria','Switzerland','United Kingdom','Portugal',
                    'Czech Republic','Czechia','Romania','Sweden','Denmark','Finland',
                    'Norway','Ireland','Hungary','Greece','Turkey','Croatia','Bulgaria',
                    'Slovakia','Slovenia','Lithuania','Latvia','Estonia','Luxembourg'];
                for (const c of countries) {
                    if (parentText.includes(c)) { result.country = c; break; }
                }
            }

            // ── ADDRESS ──
            const addrEl = document.querySelector('[class*="address" i], address');
            if (addrEl) result.address = addrEl.textContent.trim().replace(/\\s+/g, ' ');

            // ── DESCRIPTION ──
            const descEl = document.querySelector('[class*="description" i]');
            if (descEl) result.description = descEl.textContent.trim().substring(0, 500);
            if (!result.description) {
                const meta = document.querySelector('meta[name="description"]');
                if (meta) result.description = (meta.content || '').substring(0, 500);
            }

            // ── COMPANY TYPE ──
            const types = ['Manufacturer','Distributor','Service provider',
                'Wholesaler','Retailer','Subcontractor','Agent'];
            const bodyText2 = document.body.innerText;
            for (const t of types) {
                if (bodyText2.includes(t)) { result.companyType = t; break; }
            }

            return result;
        }''')

        # Merge extracted data
        for key, value in extracted.items():
            if value:
                data[key] = value.strip()

        # Clean data
        data = _clean_data(data)

    except Exception as e:
        log.error(f'Error scraping {url}: {e}')
    finally:
        await page.close()

    return data


async def _dismiss_cookies(page) -> None:
    """Dismiss cookie consent banner."""
    selectors = [
        'button:has-text("Accept")', 'button:has-text("Accept all")',
        'button:has-text("Accept All")', 'button:has-text("I agree")',
        'button:has-text("OK")', '#onetrust-accept-btn-handler',
        'button[id*="cookie" i]', '.cookie-banner button',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click(timeout=2000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def _click_reveal_buttons(page) -> None:
    """Click all buttons that reveal hidden contact info (phone, email)."""
    reveal_selectors = [
        'button:has-text("Show")', 'button:has-text("show")',
        'a:has-text("Show number")', 'a:has-text("Show Number")',
        'button:has-text("number")', 'button:has-text("phone")',
        '[data-testid*="show"]', '[class*="show-phone"]',
        '[class*="reveal"]', 'button:has-text("See")',
        'button:has-text("View")', 'a:has-text("View")',
    ]
    for sel in reveal_selectors:
        try:
            elements = page.locator(sel)
            count = await elements.count()
            for i in range(min(count, 5)):
                try:
                    el = elements.nth(i)
                    if await el.is_visible(timeout=500):
                        await el.click(timeout=2000)
                        await page.wait_for_timeout(800)
                except Exception:
                    continue
        except Exception:
            continue


def _is_company_url(url: str) -> bool:
    return bool(re.search(r'/[A-Za-z0-9][^/]*/SEAC\d+-\d+\.html', url))


def _clean_data(data: dict) -> dict:
    cleaned = {}
    for key, value in data.items():
        if isinstance(value, str):
            value = re.sub(r'\s+', ' ', value).strip()
            if key == 'companyName':
                value = re.sub(r'\s*Verified\s*', '', value).strip()
            if key == 'phoneNumber':
                value = re.sub(r'(?i)show\s*(phone|number|tel)', '', value).strip()
            if key == 'email':
                if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', value):
                    value = ''
            if key == 'website':
                if not value.startswith(('http://', 'https://')):
                    value = f'https://{value}' if '.' in value else ''
            if key == 'description':
                # Remove generic EuroPages descriptions
                if 'europages app' in value.lower() or 'supplier search' in value.lower():
                    value = ''
        cleaned[key] = value if value else ''
    return cleaned
