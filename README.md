# EuroPages Company Scraper

Scrapes company contact data from [europages.co.uk](https://www.europages.co.uk/) — Europe's leading B2B sourcing platform.

## What data does it extract?

| Field | Description |
|-------|-------------|
| **Company Name** | Official company name from the profile header |
| **Email** | Email address (from mailto links or page text) |
| **Website** | Company's own website URL |
| **Phone Number** | Phone number (reveals hidden numbers automatically) |
| **Country** | Country where the company is based |
| **Address** | Full postal address |
| **Description** | Company description / about text |
| **Company Type** | Manufacturer, Distributor, Service Provider, etc. |
| **EuroPages URL** | Source URL of the company profile |

## How to use

### Option 1: Search by keyword

Enter a search query (e.g. "construction", "furniture", "doors") and the scraper will:

1. Navigate to the EuroPages search results for that keyword
2. Collect company profile links from the listing pages
3. Visit each company profile and extract contact data

```json
{
    "searchQuery": "construction",
    "maxResults": 50,
    "maxPages": 3
}
```

### Option 2: Provide specific URLs

Supply direct URLs to EuroPages company profiles or listing pages:

```json
{
    "startUrls": [
        { "url": "https://www.europages.co.uk/companies/construction.html" },
        { "url": "https://www.europages.co.uk/FLESSYA-SRL/SEAC003605559-001.html" }
    ],
    "maxResults": 100
}
```

## Input parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `searchQuery` | string | `"construction"` | Keyword to search on EuroPages |
| `startUrls` | array | `[]` | Direct EuroPages URLs to scrape (overrides searchQuery) |
| `maxResults` | integer | `100` | Maximum number of companies to scrape (0 = unlimited) |
| `maxPages` | integer | `5` | Maximum listing pages to crawl for company links |
| `proxyConfiguration` | object | `{}` | Proxy settings (recommended for large-scale scraping) |

## Output example

```json
{
    "companyName": "MGK CONSTRUCTION",
    "email": "info@mgk-construction.com",
    "website": "https://www.mgk-construction.com",
    "phoneNumber": "+48 123 456 789",
    "country": "Poland",
    "address": "ul. Tczewska 28b, Nowy Dwor Gdanski 82-100",
    "description": "We are a Polish company specializing in the rental of professional workers...",
    "companyType": "Service provider",
    "europagesUrl": "https://www.europages.co.uk/MGK-CONSTRUCTION/SEAC005123456-001.html"
}
```

## Tips for best results

- **Use Apify Proxy** for large scraping runs to avoid rate limiting
- **Start with small `maxResults`** (10-20) to test your search query first
- **Not all companies have email/phone** listed publicly — some only offer a contact form
- The scraper automatically tries to click "Show Number" buttons to reveal hidden phone numbers

## Technical details

- Built with **Crawlee + Playwright** for full JavaScript rendering
- Handles cookie consent banners automatically
- Retries failed requests up to 3 times
- 2-minute timeout per page for slow-loading profiles
