"""EuroPages Company Scraper - Extracts contacts from company websites.

Strategy:
1. Scrape EuroPages listings to get company names + website URLs
2. Visit each company's own website to extract phone & email from footer/contact sections

Uses Playwright for browser rendering to handle dynamic company websites.
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
    results = await run_scraper(actor_input)

    # Push results to Apify dataset
    if apify_token and dataset_id and results:
        async with httpx.AsyncClient() as api_client:
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

    log.info(f'Done! {len(results)} leads with contact info.')


async def run_scraper(actor_input: dict) -> list[dict]:
    """Run the two-step scraper: EuroPages → Company websites."""
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

        # ─── STEP 1: Collect company data from EuroPages ───
        if start_urls:
            listing_urls = []
            for url_item in start_urls:
                url = url_item.get('url', url_item) if isinstance(url_item, dict) else str(url_item)
                listing_urls.append(url)
        else:
            listing_urls = [f'{BASE_URL}/companies/{quote_plus(search_query)}.html']

        companies = []
        for listing_url in listing_urls:
            if _is_company_url(listing_url):
                companies.append({'europagesUrl': listing_url})
            else:
                found = await scrape_europages_listings(context, listing_url, max_pages, max_results - len(companies))
                companies.extend(found)
                if 0 < max_results <= len(companies):
                    companies = companies[:max_results]
                    break

        log.info(f'═══ Step 1 complete: {len(companies)} companies from EuroPages ═══')

        # ─── STEP 2: Visit each company's EuroPages page to get website + basic info ───
        for i, company in enumerate(companies):
            if 0 < max_results <= len(results):
                break
            try:
                log.info(f'[{i+1}/{len(companies)}] EuroPages: {company["europagesUrl"]}')
                ep_data = await scrape_europages_detail(context, company['europagesUrl'])
                company.update(ep_data)

                if not company.get('website'):
                    log.warning(f'  ✗ No website found, skipping')
                    continue

                log.info(f'  Company: {company.get("companyName", "?")} | Website: {company["website"]}')

                # ─── STEP 3: Visit the company's OWN website to get phone & email ───
                log.info(f'  → Visiting company website: {company["website"]}')
                contacts = await scrape_company_website(context, company['website'])
                company['email'] = contacts.get('email', '')
                company['phone'] = contacts.get('phone', '')

                # Use website contacts as fallback for address
                if not company.get('address') and contacts.get('address'):
                    company['address'] = contacts['address']

                company = _clean_data(company)
                results.append(company)

                log.info(
                    f'  ✓ {company.get("companyName", "?")} | '
                    f'Phone: {company.get("phone") or "—"} | '
                    f'Email: {company.get("email") or "—"}'
                )

            except Exception as e:
                log.error(f'  ✗ Error: {e}')

        await browser.close()

    return results


# ─── EUROPAGES SCRAPING ───────────────────────────────────────────────────────

async def scrape_europages_listings(context, base_url: str, max_pages: int, max_items: int) -> list[dict]:
    """Scrape EuroPages listing pages to collect company URLs."""
    companies = []
    page = await context.new_page()

    try:
        for page_num in range(1, max_pages + 1):
            if 0 < max_items <= len(companies):
                break

            url = base_url if page_num == 1 else f'{base_url}{"&" if "?" in base_url else "?"}page={page_num}'
            log.info(f'Listing page {page_num}: {url}')

            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                log.error(f'Failed to load listing: {e}')
                break

            await _dismiss_cookies(page)

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
                if not any(c['europagesUrl'] == link for c in companies):
                    companies.append({'europagesUrl': link})

            if len(links) == 0:
                break

    finally:
        await page.close()

    return companies


async def scrape_europages_detail(context, url: str) -> dict:
    """Scrape a EuroPages company page for basic info + website URL."""
    page = await context.new_page()
    data = {}

    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(2000)
        await _dismiss_cookies(page)

        data = await page.evaluate('''() => {
            const result = {
                companyName: '', website: '', country: '',
                address: '', companyType: '', description: '',
            };

            // Company name from h1
            const h1 = document.querySelector('h1');
            if (h1) {
                let name = '';
                for (const node of h1.childNodes) {
                    if (node.nodeType === Node.TEXT_NODE) name += node.textContent;
                }
                if (!name) name = h1.textContent;
                result.companyName = name.replace(/Verified/gi, '').replace(/✓/g, '').trim();
            }

            // Website - look for external links (the "Visit website" button)
            const skipDomains = ['europages', 'google', 'facebook', 'linkedin',
                'twitter', 'instagram', 'youtube', 'maps.google', 'apple.com',
                'play.google'];

            // First try: links opening in new tab (Visit website button)
            for (const a of document.querySelectorAll('a[target="_blank"][href^="http"]')) {
                const href = a.href;
                if (!skipDomains.some(d => href.includes(d))) {
                    result.website = href;
                    break;
                }
            }

            // Second try: any external link not to excluded domains
            if (!result.website) {
                for (const a of document.querySelectorAll('a[href^="http"]')) {
                    const href = a.href;
                    const text = (a.textContent || '').toLowerCase();
                    if ((text.includes('website') || text.includes('visit') || text.includes('www'))
                        && !skipDomains.some(d => href.includes(d))) {
                        result.website = href;
                        break;
                    }
                }
            }

            // Country
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

            // Address from header area
            const addrEls = document.querySelectorAll('.font-copy-400.text-neutral-100');
            for (const el of addrEls) {
                const text = el.textContent.trim();
                if (text && !text.includes('Manufacturer') && !text.includes('Distributor')
                    && !text.includes('Service provider') && !text.includes('Wholesaler')
                    && !text.includes('Retailer') && (text.includes(',') || /\\d{4,5}/.test(text))) {
                    result.address = text.replace(/\\s+/g, ' ');
                    break;
                }
            }

            // Company type
            const types = ['Manufacturer','Distributor','Service provider',
                'Wholesaler','Retailer','Subcontractor','Agent'];
            const bodyText = document.body.innerText;
            for (const t of types) {
                if (bodyText.includes(t)) { result.companyType = t; break; }
            }

            // Description from meta
            const meta = document.querySelector('meta[name="description"]');
            if (meta && meta.content) {
                const desc = meta.content.trim();
                if (!desc.includes('europages app') && !desc.includes('supplier search')) {
                    result.description = desc.substring(0, 500);
                }
            }

            return result;
        }''')

    except Exception as e:
        log.error(f'Error scraping EuroPages detail: {e}')
    finally:
        await page.close()

    return data


# ─── COMPANY WEBSITE SCRAPING ─────────────────────────────────────────────────

async def scrape_company_website(context, website_url: str) -> dict:
    """Visit a company's own website and extract phone & email from footer/contact page."""
    contacts = {'email': '', 'phone': '', 'address': ''}
    page = await context.new_page()

    try:
        # Visit the company website
        await page.goto(website_url, wait_until='domcontentloaded', timeout=20000)
        await page.wait_for_timeout(3000)

        # Try dismissing cookie banners on the company site too
        await _dismiss_cookies(page)
        await page.wait_for_timeout(1000)

        # Scroll to the bottom to load footer content
        await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        await page.wait_for_timeout(2000)

        # Extract contacts from the page (especially footer)
        page_contacts = await _extract_contacts_from_page(page)
        if page_contacts.get('email'):
            contacts['email'] = page_contacts['email']
        if page_contacts.get('phone'):
            contacts['phone'] = page_contacts['phone']
        if page_contacts.get('address'):
            contacts['address'] = page_contacts['address']

        # If no email/phone found, try visiting /contact or /contacts page
        if not contacts['email'] or not contacts['phone']:
            contact_page_url = await _find_contact_page_link(page)
            if contact_page_url:
                log.info(f'    → Visiting contact page: {contact_page_url}')
                try:
                    await page.goto(contact_page_url, wait_until='domcontentloaded', timeout=15000)
                    await page.wait_for_timeout(2000)
                    await _dismiss_cookies(page)

                    contact_page_data = await _extract_contacts_from_page(page)
                    if not contacts['email'] and contact_page_data.get('email'):
                        contacts['email'] = contact_page_data['email']
                    if not contacts['phone'] and contact_page_data.get('phone'):
                        contacts['phone'] = contact_page_data['phone']
                    if not contacts['address'] and contact_page_data.get('address'):
                        contacts['address'] = contact_page_data['address']
                except Exception:
                    pass

    except Exception as e:
        log.warning(f'    Failed to scrape website {website_url}: {e}')
    finally:
        await page.close()

    return contacts


async def _extract_contacts_from_page(page) -> dict:
    """Extract email, phone, and address from any web page."""
    return await page.evaluate('''() => {
        const result = { email: '', phone: '', address: '' };

        const excludeEmailDomains = [
            'europages', 'sentry.io', 'googleapis', 'google.com',
            'facebook.com', 'hotjar.com', 'segment.io', 'mixpanel',
            'intercom', 'cloudflare', 'example.com', 'wixpress',
            'w3.org', 'schema.org', 'gravatar.com', 'wordpress',
        ];

        // ── EMAIL ──
        // Priority 1: mailto links
        for (const a of document.querySelectorAll('a[href^="mailto:"]')) {
            const email = a.href.replace('mailto:', '').split('?')[0].trim().toLowerCase();
            if (email && email.includes('@') && !excludeEmailDomains.some(d => email.includes(d))) {
                result.email = email;
                break;
            }
        }

        // Priority 2: email text in footer
        if (!result.email) {
            const footerEls = document.querySelectorAll('footer, [class*="footer" i], [id*="footer" i], [class*="contact" i]');
            const emailRegex = /[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}/g;
            for (const el of footerEls) {
                const matches = el.innerText.match(emailRegex) || [];
                for (const m of matches) {
                    if (!excludeEmailDomains.some(d => m.toLowerCase().includes(d))) {
                        result.email = m.toLowerCase();
                        break;
                    }
                }
                if (result.email) break;
            }
        }

        // Priority 3: email anywhere on page
        if (!result.email) {
            const allText = document.body.innerText;
            const emailRegex = /[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}/g;
            const matches = allText.match(emailRegex) || [];
            for (const m of matches) {
                if (!excludeEmailDomains.some(d => m.toLowerCase().includes(d))) {
                    result.email = m.toLowerCase();
                    break;
                }
            }
        }

        // ── PHONE ──
        // Priority 1: tel: links
        for (const a of document.querySelectorAll('a[href^="tel:"]')) {
            const phone = a.href.replace('tel:', '').replace(/%20/g, ' ').trim();
            if (phone && phone.replace(/\\D/g, '').length >= 7) {
                result.phone = phone;
                break;
            }
        }

        // Priority 2: phone text in footer/contact sections
        if (!result.phone) {
            const contactEls = document.querySelectorAll(
                'footer, [class*="footer" i], [id*="footer" i], [class*="contact" i], ' +
                '[class*="phone" i], [class*="tel" i]'
            );
            for (const el of contactEls) {
                const text = el.innerText;
                // Match international phone formats
                const phoneMatch = text.match(/(?:Phone|Tel|Telephone|Fax|Mobile|Call)[:\\s]*([+\\d][\\d\\s()\\-\\.]{6,24})/i);
                if (phoneMatch) {
                    const phone = phoneMatch[1].trim();
                    if (phone.replace(/\\D/g, '').length >= 7) {
                        result.phone = phone;
                        break;
                    }
                }
            }
        }

        // Priority 3: any tel: pattern or international phone in visible text
        if (!result.phone) {
            const bodyText = document.body.innerText;
            const intlMatch = bodyText.match(/(?:^|\\s)(\\+\\d{1,3}[\\s\\-\\.]?\\(?\\d{1,4}\\)?[\\s\\-\\.]?\\d{2,10}[\\s\\-\\.]?\\d{0,10})(?:\\s|$)/m);
            if (intlMatch) {
                const phone = intlMatch[1].trim();
                if (phone.replace(/\\D/g, '').length >= 7) {
                    result.phone = phone;
                }
            }
        }

        // ── ADDRESS ──
        // Check footer for address patterns
        const footerArea = document.querySelector('footer, [class*="footer" i]');
        if (footerArea) {
            const addrEl = footerArea.querySelector('address, [class*="address" i]');
            if (addrEl) {
                result.address = addrEl.textContent.trim().replace(/\\s+/g, ' ').substring(0, 200);
            }
        }

        return result;
    }''')


async def _find_contact_page_link(page) -> str | None:
    """Find a link to the Contact page on a company website."""
    return await page.evaluate('''() => {
        const contactKeywords = ['contact', 'contatti', 'kontakt', 'contacto',
            'nous contacter', 'contactez', 'get in touch', 'reach us'];

        for (const a of document.querySelectorAll('a[href]')) {
            const text = (a.textContent || '').toLowerCase().trim();
            const href = (a.getAttribute('href') || '').toLowerCase();

            for (const keyword of contactKeywords) {
                if (text.includes(keyword) || href.includes(keyword)) {
                    const fullUrl = a.href; // browser resolves relative URLs
                    if (fullUrl && fullUrl.startsWith('http')) {
                        return fullUrl;
                    }
                }
            }
        }
        return null;
    }''')


# ─── UTILITIES ────────────────────────────────────────────────────────────────

async def _dismiss_cookies(page) -> None:
    """Dismiss cookie consent banner."""
    selectors = [
        'button:has-text("Accept")', 'button:has-text("Accept all")',
        'button:has-text("Accept All")', 'button:has-text("I agree")',
        'button:has-text("OK")', 'button:has-text("Accetta")',
        'button:has-text("Akzeptieren")', 'button:has-text("Aceptar")',
        '#onetrust-accept-btn-handler', 'button[id*="cookie" i]',
        '.cookie-banner button', '[class*="cookie"] button',
        'button:has-text("Agree")', 'button:has-text("Got it")',
        'button:has-text("Allow")', 'button:has-text("Consent")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=2000)
                await page.wait_for_timeout(500)
                return
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
            if key == 'phone':
                value = re.sub(r'(?i)show\s*(phone|number|tel)', '', value).strip()
            if key == 'email':
                if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', value):
                    value = ''
            if key == 'website':
                if not value.startswith(('http://', 'https://')):
                    value = f'https://{value}' if '.' in value else ''
            if key == 'description':
                if 'europages app' in value.lower() or 'supplier search' in value.lower():
                    value = ''
        cleaned[key] = value if value else ''
    return cleaned
