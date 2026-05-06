"""EuroPages Company Scraper - Main entry point.

Scrapes company data from europages.co.uk including:
- Company name
- Email address
- Website URL
- Phone number
- Country, address, description, and company type

Uses Playwright for rendering JavaScript-heavy pages and handling
dynamic content like "Show phone number" buttons.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from crawlee.playwright_crawler import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.configuration import Configuration

# Base URL for EuroPages
BASE_URL = 'https://www.europages.co.uk'

# Labels for routing requests
LABEL_LISTING = 'LISTING'
LABEL_DETAIL = 'DETAIL'


async def main() -> None:
    """Main entry point for the Apify Actor."""
    async with Actor:
        actor_input = await Actor.get_input() or {}

        search_query = actor_input.get('searchQuery', 'construction')
        start_urls = actor_input.get('startUrls', [])
        max_results = actor_input.get('maxResults', 100)
        max_pages = actor_input.get('maxPages', 5)
        proxy_config = actor_input.get('proxyConfiguration', {})

        # Track how many results we've scraped
        results_count = 0

        # Configure Crawlee
        config = Configuration(
            persist_storage=True,
        )

        # Build proxy configuration for Crawlee
        proxy_url = None
        if proxy_config and proxy_config.get('useApifyProxy'):
            proxy_url = None  # Crawlee handles Apify proxy automatically

        crawler = PlaywrightCrawler(
            headless=True,
            browser_type='chromium',
            max_request_retries=3,
            request_handler_timeout=120_000,  # 2 minutes per page
            max_requests_per_crawl=max_results + (max_pages * 2) + 50 if max_results > 0 else 0,
            configuration=config,
        )

        @crawler.router.default_handler
        async def default_handler(context: PlaywrightCrawlingContext) -> None:
            """Route requests based on their label."""
            label = context.request.label or ''
            url = context.request.url

            if label == LABEL_DETAIL:
                await handle_detail_page(context)
            elif label == LABEL_LISTING:
                await handle_listing_page(context, max_pages)
            else:
                # Auto-detect: if URL looks like a company page, treat as detail
                if _is_company_url(url):
                    await handle_detail_page(context)
                else:
                    await handle_listing_page(context, max_pages)

        async def handle_listing_page(
            context: PlaywrightCrawlingContext,
            max_listing_pages: int,
        ) -> None:
            """Extract company links from a search results / listing page."""
            nonlocal results_count

            Actor.log.info(f'Processing listing page: {context.request.url}')

            page = context.page

            # Wait for the page to be fully loaded
            try:
                await page.wait_for_load_state('networkidle', timeout=30000)
            except Exception:
                await page.wait_for_load_state('domcontentloaded', timeout=15000)

            # Accept cookies if the banner appears
            await _dismiss_cookie_banner(page)

            # Wait a bit for dynamic content
            await page.wait_for_timeout(2000)

            # Find company links on the listing page
            # EuroPages company detail URLs follow pattern: /COMPANY-NAME/SEACXXXXX-XXX.html
            company_links = await page.evaluate('''() => {
                const links = [];
                const allLinks = document.querySelectorAll('a[href]');
                for (const link of allLinks) {
                    const href = link.getAttribute('href');
                    if (href && /\\/[A-Z0-9][^/]*\\/SEAC[0-9]+-[0-9]+\\.html/.test(href)) {
                        const fullUrl = href.startsWith('http')
                            ? href
                            : window.location.origin + href;
                        if (!links.includes(fullUrl)) {
                            links.push(fullUrl);
                        }
                    }
                }
                return links;
            }''')

            Actor.log.info(f'Found {len(company_links)} company links on listing page')

            # Enqueue company detail pages
            for link in company_links:
                if max_results > 0 and results_count >= max_results:
                    Actor.log.info(f'Reached max results limit ({max_results})')
                    break
                results_count += 1
                await context.add_requests([{
                    'url': link,
                    'label': LABEL_DETAIL,
                }])

            # Handle pagination - find "Next" page link
            current_url = context.request.url
            current_page_num = _extract_page_number(current_url)

            if current_page_num < max_listing_pages:
                next_page_url = await page.evaluate('''() => {
                    // Look for next page links
                    const nextBtns = document.querySelectorAll(
                        'a[rel="next"], a.pagination-next, a[aria-label="Next"], ' +
                        'button[aria-label="Next"], a[data-testid*="next"]'
                    );
                    for (const btn of nextBtns) {
                        if (btn.href) return btn.href;
                    }

                    // Look for numbered pagination links
                    const paginationLinks = document.querySelectorAll(
                        'nav a[href], .pagination a[href], [class*="pagination"] a[href]'
                    );
                    const currentPage = ''' + str(current_page_num) + ''';
                    for (const link of paginationLinks) {
                        const text = link.textContent.trim();
                        if (text === String(currentPage + 1)) {
                            return link.href;
                        }
                    }

                    return null;
                }''')

                if next_page_url:
                    Actor.log.info(f'Found next page: {next_page_url}')
                    await context.add_requests([{
                        'url': next_page_url,
                        'label': LABEL_LISTING,
                    }])
                else:
                    # Try constructing the next page URL
                    next_url = _build_next_page_url(current_url, current_page_num + 1)
                    if next_url:
                        Actor.log.info(f'Constructed next page URL: {next_url}')
                        await context.add_requests([{
                            'url': next_url,
                            'label': LABEL_LISTING,
                        }])

        async def handle_detail_page(context: PlaywrightCrawlingContext) -> None:
            """Extract company details from a company profile page."""
            Actor.log.info(f'Processing company page: {context.request.url}')

            page = context.page

            # Wait for the page to load
            try:
                await page.wait_for_load_state('networkidle', timeout=30000)
            except Exception:
                await page.wait_for_load_state('domcontentloaded', timeout=15000)

            # Accept cookies if needed
            await _dismiss_cookie_banner(page)

            # Wait for dynamic content
            await page.wait_for_timeout(2000)

            # Try to reveal hidden contact info (phone numbers, etc.)
            await _reveal_contact_info(page)

            # Extract all company data from the page
            company_data = await page.evaluate('''() => {
                const data = {
                    companyName: '',
                    email: '',
                    website: '',
                    phoneNumber: '',
                    country: '',
                    address: '',
                    description: '',
                    companyType: '',
                };

                // --- COMPANY NAME ---
                // Try multiple selectors for the company name
                const nameSelectors = [
                    'h1',
                    '[data-testid="company-name"]',
                    '.company-name',
                    '.company-header h1',
                    '.header-company-name',
                ];
                for (const sel of nameSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        // Get just the text, excluding child badges like "Verified"
                        let name = '';
                        for (const child of el.childNodes) {
                            if (child.nodeType === Node.TEXT_NODE) {
                                name += child.textContent.trim();
                            }
                        }
                        if (!name) name = el.textContent.trim();
                        // Clean up - remove "Verified" badge text
                        name = name.replace(/\\s*Verified\\s*/g, '').trim();
                        if (name) {
                            data.companyName = name;
                            break;
                        }
                    }
                }

                // --- EMAIL ---
                // Look for mailto: links
                const mailtoLinks = document.querySelectorAll('a[href^="mailto:"]');
                for (const link of mailtoLinks) {
                    const email = link.getAttribute('href').replace('mailto:', '').split('?')[0].trim();
                    if (email && email.includes('@')) {
                        data.email = email;
                        break;
                    }
                }

                // If no mailto, look for email text patterns in contact sections
                if (!data.email) {
                    const contactSections = document.querySelectorAll(
                        '[class*="contact"], [class*="info"], [data-testid*="contact"]'
                    );
                    const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/;
                    for (const section of contactSections) {
                        const match = section.textContent.match(emailRegex);
                        if (match) {
                            data.email = match[0];
                            break;
                        }
                    }
                }

                // Also check entire page for email if still not found
                if (!data.email) {
                    const bodyText = document.body.innerText;
                    const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/;
                    const match = bodyText.match(emailRegex);
                    if (match) {
                        // Filter out common false positives
                        const email = match[0];
                        if (!email.includes('europages') && !email.includes('example.com')) {
                            data.email = email;
                        }
                    }
                }

                // --- WEBSITE ---
                // Look for external website links
                const websiteSelectors = [
                    'a[data-testid*="website"]',
                    'a[class*="website"]',
                    'a[rel="nofollow noopener"][target="_blank"]',
                ];
                for (const sel of websiteSelectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const href = el.getAttribute('href');
                        if (href && !href.includes('europages') &&
                            !href.includes('google') && !href.includes('facebook') &&
                            !href.includes('linkedin') && !href.includes('twitter') &&
                            !href.includes('instagram') && !href.includes('youtube') &&
                            (href.startsWith('http://') || href.startsWith('https://'))) {
                            data.website = href;
                            break;
                        }
                    }
                    if (data.website) break;
                }

                // Fallback: look for links with globe/website icons or text "Website"
                if (!data.website) {
                    const allLinks = document.querySelectorAll('a[href]');
                    for (const link of allLinks) {
                        const text = link.textContent.trim().toLowerCase();
                        const href = link.getAttribute('href');
                        if ((text.includes('visit website') || text.includes('website') ||
                             text.includes('web site') || text === 'www') &&
                            href && !href.includes('europages') &&
                            (href.startsWith('http://') || href.startsWith('https://'))) {
                            data.website = href;
                            break;
                        }
                    }
                }

                // --- PHONE NUMBER ---
                // Look for tel: links
                const telLinks = document.querySelectorAll('a[href^="tel:"]');
                for (const link of telLinks) {
                    const phone = link.getAttribute('href').replace('tel:', '').trim();
                    if (phone) {
                        data.phoneNumber = phone;
                        break;
                    }
                }

                // Look for phone number in visible text near phone-related elements
                if (!data.phoneNumber) {
                    const phoneContainers = document.querySelectorAll(
                        '[class*="phone"], [class*="tel"], [data-testid*="phone"]'
                    );
                    const phoneRegex = /[+]?[\\d\\s().-]{7,20}/;
                    for (const container of phoneContainers) {
                        const match = container.textContent.match(phoneRegex);
                        if (match) {
                            data.phoneNumber = match[0].trim();
                            break;
                        }
                    }
                }

                // --- COUNTRY ---
                // Look for country flags or country text
                const countrySelectors = [
                    '[class*="country"]',
                    '[data-testid*="country"]',
                ];
                for (const sel of countrySelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        data.country = el.textContent.trim();
                        break;
                    }
                }

                // Fallback: look for flag images with alt text
                if (!data.country) {
                    const flags = document.querySelectorAll('img[alt]');
                    const countries = [
                        'Germany', 'France', 'Italy', 'Spain', 'Poland', 'Netherlands',
                        'Belgium', 'Austria', 'Switzerland', 'United Kingdom', 'Portugal',
                        'Czech Republic', 'Romania', 'Sweden', 'Denmark', 'Finland',
                        'Norway', 'Ireland', 'Hungary', 'Greece', 'Turkey',
                    ];
                    for (const flag of flags) {
                        const alt = flag.getAttribute('alt');
                        if (alt && countries.some(c => alt.includes(c))) {
                            data.country = alt.trim();
                            break;
                        }
                    }
                }

                // --- ADDRESS ---
                // Look for address-related elements
                const addressSelectors = [
                    '[class*="address"]',
                    '[data-testid*="address"]',
                    'address',
                ];
                for (const sel of addressSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        data.address = el.textContent.trim().replace(/\\s+/g, ' ');
                        break;
                    }
                }

                // Fallback: look for location info near the company header
                if (!data.address) {
                    const headerArea = document.querySelector(
                        '[class*="header"], [class*="company-info"], [class*="CompanyHeader"]'
                    );
                    if (headerArea) {
                        // Find text that looks like an address (contains postal code pattern)
                        const postalRegex = /[A-Z]{0,2}[\\s-]?\\d{4,5}[\\s,]/;
                        const text = headerArea.textContent;
                        const match = text.match(postalRegex);
                        if (match) {
                            // Get the surrounding context
                            const idx = text.indexOf(match[0]);
                            const start = Math.max(0, text.lastIndexOf('\\n', idx));
                            const end = text.indexOf('\\n', idx + match[0].length);
                            data.address = text.substring(start, end > 0 ? end : undefined)
                                .trim().replace(/\\s+/g, ' ');
                        }
                    }
                }

                // --- DESCRIPTION ---
                const descSelectors = [
                    '[class*="description"]',
                    '[data-testid*="description"]',
                    'meta[name="description"]',
                ];
                for (const sel of descSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        if (el.tagName === 'META') {
                            data.description = el.getAttribute('content') || '';
                        } else {
                            data.description = el.textContent.trim().substring(0, 500);
                        }
                        if (data.description) break;
                    }
                }

                // --- COMPANY TYPE ---
                // EuroPages shows types like "Manufacturer", "Distributor", "Service provider"
                const typeKeywords = [
                    'Manufacturer', 'Distributor', 'Service provider',
                    'Wholesaler', 'Retailer', 'Subcontractor', 'Agent',
                ];
                const pageText = document.body.innerText;
                for (const keyword of typeKeywords) {
                    if (pageText.includes(keyword)) {
                        data.companyType = keyword;
                        break;
                    }
                }

                return data;
            }''')

            # Also try to extract additional info using Playwright-specific methods
            # Try clicking "Show Number" buttons to reveal phone numbers
            if not company_data.get('phoneNumber'):
                phone = await _try_extract_phone_from_page(page)
                if phone:
                    company_data['phoneNumber'] = phone

            # Try extracting website from the Company Information sidebar
            if not company_data.get('website'):
                website = await _try_extract_website_from_sidebar(page)
                if website:
                    company_data['website'] = website

            # Try extracting country from the header area text
            if not company_data.get('country'):
                country = await _try_extract_country(page)
                if country:
                    company_data['country'] = country

            # Try extracting address from the header area
            if not company_data.get('address'):
                address = await _try_extract_address(page)
                if address:
                    company_data['address'] = address

            # Add the source URL
            company_data['europagesUrl'] = context.request.url

            # Clean up the data
            company_data = _clean_company_data(company_data)

            # Only push if we have at least a company name
            if company_data.get('companyName'):
                Actor.log.info(
                    f'Scraped: {company_data["companyName"]} | '
                    f'Phone: {company_data.get("phoneNumber", "N/A")} | '
                    f'Email: {company_data.get("email", "N/A")} | '
                    f'Website: {company_data.get("website", "N/A")}'
                )
                await Actor.push_data(company_data)
            else:
                Actor.log.warning(f'No company name found on {context.request.url}')

        # Build the initial request list
        requests = []

        if start_urls:
            # Use user-provided URLs
            for url_item in start_urls:
                url = url_item.get('url', url_item) if isinstance(url_item, dict) else str(url_item)
                if _is_company_url(url):
                    requests.append({'url': url, 'label': LABEL_DETAIL})
                else:
                    requests.append({'url': url, 'label': LABEL_LISTING})
        else:
            # Build search URL from query
            search_url = f'{BASE_URL}/companies/{quote_plus(search_query)}.html'
            Actor.log.info(f'Starting search with URL: {search_url}')
            requests.append({'url': search_url, 'label': LABEL_LISTING})

        # Run the crawler
        Actor.log.info(f'Starting crawl with {len(requests)} initial URLs')
        await crawler.run(requests)
        Actor.log.info('Crawl finished!')


# ─── Helper Functions ─────────────────────────────────────────────────────────

async def _dismiss_cookie_banner(page) -> None:
    """Try to dismiss the cookie consent banner if present."""
    try:
        cookie_selectors = [
            'button[id*="cookie" i][id*="accept" i]',
            'button[class*="cookie" i][class*="accept" i]',
            'button:has-text("Accept")',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("I agree")',
            'button:has-text("OK")',
            '#onetrust-accept-btn-handler',
            '[data-testid*="cookie"] button',
            '.cookie-banner button',
        ]
        for selector in cookie_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=1000):
                    await btn.click(timeout=2000)
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue
    except Exception:
        pass


async def _reveal_contact_info(page) -> None:
    """Try to click buttons that reveal hidden contact information."""
    reveal_selectors = [
        'button:has-text("Show Number")',
        'button:has-text("Show number")',
        'button:has-text("Show phone")',
        'button:has-text("Show Phone")',
        'a:has-text("Show Number")',
        'a:has-text("Show number")',
        '[data-testid*="show-phone"]',
        '[class*="show-phone"]',
        '[class*="reveal"]',
    ]
    for selector in reveal_selectors:
        try:
            elements = page.locator(selector)
            count = await elements.count()
            for i in range(min(count, 3)):
                try:
                    el = elements.nth(i)
                    if await el.is_visible(timeout=1000):
                        await el.click(timeout=2000)
                        await page.wait_for_timeout(1000)
                except Exception:
                    continue
        except Exception:
            continue


async def _try_extract_phone_from_page(page) -> str | None:
    """Try multiple strategies to extract phone number."""
    try:
        # Check for tel: links after reveal
        tel_link = page.locator('a[href^="tel:"]').first
        if await tel_link.count() > 0:
            href = await tel_link.get_attribute('href')
            if href:
                return href.replace('tel:', '').strip()

        # Check for phone patterns in Company Information section
        info_section = page.locator('text=Company Information').first
        if await info_section.count() > 0:
            parent = info_section.locator('..')
            text = await parent.inner_text()
            phone_match = re.search(r'(?:Phone|Tel|Telephone)[:\s]*([+\d\s().-]{7,20})', text, re.IGNORECASE)
            if phone_match:
                return phone_match.group(1).strip()

        return None
    except Exception:
        return None


async def _try_extract_website_from_sidebar(page) -> str | None:
    """Try to extract website URL from the sidebar or company info section."""
    try:
        # Look for external links in the company info area
        links = page.locator('a[target="_blank"][rel*="nofollow"]')
        count = await links.count()
        for i in range(count):
            href = await links.nth(i).get_attribute('href')
            if href and not any(domain in href for domain in [
                'europages', 'google', 'facebook', 'linkedin',
                'twitter', 'instagram', 'youtube', 'maps.google'
            ]):
                return href
        return None
    except Exception:
        return None


async def _try_extract_country(page) -> str | None:
    """Try to extract country from the page header."""
    try:
        # Look for flag emojis or country text near the company header
        header = page.locator('h1').first
        if await header.count() > 0:
            parent = header.locator('..')
            text = await parent.inner_text()
            countries = [
                'Germany', 'France', 'Italy', 'Spain', 'Poland', 'Netherlands',
                'Belgium', 'Austria', 'Switzerland', 'United Kingdom', 'Portugal',
                'Czech Republic', 'Czechia', 'Romania', 'Sweden', 'Denmark',
                'Finland', 'Norway', 'Ireland', 'Hungary', 'Greece', 'Turkey',
                'Croatia', 'Bulgaria', 'Slovakia', 'Slovenia', 'Lithuania',
                'Latvia', 'Estonia', 'Luxembourg', 'Malta', 'Cyprus',
            ]
            for country in countries:
                if country in text:
                    return country
        return None
    except Exception:
        return None


async def _try_extract_address(page) -> str | None:
    """Try to extract address from the header or company info section."""
    try:
        # EuroPages shows address near the country flag
        header_area = page.locator('h1').first.locator('..')
        if await header_area.count() > 0:
            text = await header_area.inner_text()
            # Look for text after country name that contains postal code patterns
            addr_match = re.search(
                r'(?:[\w\s]+,\s+)?[\w\s.]+\d{4,5}[\s,-]+[\w\s]+',
                text
            )
            if addr_match:
                return addr_match.group(0).strip()

        # Try the Company Information sidebar
        info_text = await page.locator('text=Location').first.locator('..').inner_text()
        if info_text and 'Location' in info_text:
            addr = info_text.replace('Location', '').strip()
            if addr:
                return addr

        return None
    except Exception:
        return None


def _is_company_url(url: str) -> bool:
    """Check if a URL is a EuroPages company detail page."""
    return bool(re.search(r'/[A-Z0-9][^/]*/SEAC\d+-\d+\.html', url))


def _extract_page_number(url: str) -> int:
    """Extract the current page number from a URL."""
    match = re.search(r'[?&]page=(\d+)', url)
    if match:
        return int(match.group(1))
    # Some URLs use /pg-N/ pattern
    match = re.search(r'/pg-(\d+)/', url)
    if match:
        return int(match.group(1))
    return 1


def _build_next_page_url(current_url: str, next_page: int) -> str | None:
    """Build the URL for the next listing page."""
    if 'page=' in current_url:
        return re.sub(r'page=\d+', f'page={next_page}', current_url)
    elif '?' in current_url:
        return f'{current_url}&page={next_page}'
    else:
        # Remove .html and add page parameter
        if current_url.endswith('.html'):
            return f'{current_url}?page={next_page}'
        return f'{current_url}?page={next_page}'


def _clean_company_data(data: dict) -> dict:
    """Clean and validate scraped company data."""
    cleaned = {}

    for key, value in data.items():
        if isinstance(value, str):
            # Strip whitespace and normalize
            value = value.strip()
            value = re.sub(r'\s+', ' ', value)

            # Remove common noise
            if key == 'companyName':
                value = re.sub(r'\s*Verified\s*', '', value).strip()
                value = re.sub(r'\s*✓\s*', '', value).strip()

            if key == 'phoneNumber':
                # Clean phone number format
                value = value.strip()
                # Remove "Show Number" or similar text
                value = re.sub(r'(?i)show\s*(phone|number|tel)', '', value).strip()

            if key == 'email':
                # Validate email format
                if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', value):
                    value = ''

            if key == 'website':
                # Ensure it's a valid URL
                if not value.startswith(('http://', 'https://')):
                    if value and '.' in value:
                        value = f'https://{value}'
                    else:
                        value = ''

        cleaned[key] = value if value else ''

    return cleaned
