# Chrome Extension Installation Guide

## Quick Start

### Step 1: Verify Extension Files

Your extension is located at:
```
C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply\extension
```

Required files present:
- [x] `manifest.json` - Extension configuration
- [x] `popup.html` - Settings UI
- [x] `popup.js` - Settings logic
- [x] `content.js` - Form automation
- [x] `gemini.js` - Gemini API client
- [x] `background.js` - Service worker

### Step 2: Load Extension in Chrome

1. **Open Chrome Extensions Page**:
   - Go to `chrome://extensions/` in your address bar
   - Or: Menu → More tools → Extensions

2. **Enable Developer Mode**:
   - Look for the toggle labeled "Developer mode" in the top-right corner
   - Click to enable it (it should turn blue)

3. **Load Unpacked Extension**:
   - Click the "Load unpacked" button that appears
   - A file browser dialog will open
   - Navigate to and select the **`extension` folder** (NOT the parent directory)
   - **Full path to select**:
     ```
     C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply\extension
     ```

4. **Verify Installation**:
   - Extension should appear in your extensions list
   - Icon will show in your toolbar (click to configure)

### Step 3: Configure Extension

1. **Click the extension icon** in your Chrome toolbar
2. **Add Gemini API Key**:
   - Get free API key from: https://aistudio.google.com/app/apikey
   - Paste into "API Key" field
3. **Add Your Resume**:
   - Paste your resume/professional summary into the textarea
4. **Click "Save Settings"**

### Step 4: Start Using

Visit any job application site:
- LinkedIn Jobs
- Workday
- Naukri.com
- Greenhouse
- Lever
- Other ATS platforms

Look for:
- **"✨" buttons** next to each form field (click to fill)
- **"⚡ Fill All" button** (bottom-right corner) to fill entire form

## Troubleshooting

### "Manifest file is missing or unreadable"

**Solution**: Make sure you're selecting the `extension` folder itself, not the parent directory.

Correct path:
```
C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply\extension
```

Not this:
```
C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply
```

### Extension doesn't appear after loading

- Refresh the `chrome://extensions/` page
- Make sure Developer Mode is enabled (blue toggle in top-right)
- Check that all files are in the `extension/` folder

### "API Key not found" on job sites

1. Click the extension icon in your toolbar
2. Verify API Key is saved in popup
3. Click "Save Settings" again
4. Refresh the job site page

### Extension icon doesn't show in toolbar

- Go to `chrome://extensions/`
- Find "AI Job Application Autofiller"
- Click the pin icon to pin it to toolbar
- Or click the menu icon to access it

## File Structure

```
extension/
├── manifest.json        ← Extension configuration (MV3)
├── popup.html          ← Settings UI
├── popup.js            ← Settings logic
├── content.js          ← Form automation engine
├── gemini.js           ← Gemini API wrapper
├── background.js       ← Service worker
├── .env.example        ← Credentials template
├── resume.txt.example  ← Resume template
└── INSTALL.md          ← This file
```

## Permissions Used

- **activeTab**: Access current tab (to see form fields)
- **scripting**: Inject content scripts into pages
- **storage**: Save your configuration locally
- **Host permissions**: `https://generativelanguage.googleapis.com/*` (Gemini API)

**Note**: All data is stored locally in Chrome. Your API key and resume never leave your computer except for API calls to Gemini.

## Getting Help

- Check the extension console for errors: `F12` → Console tab
- Run diagnostic: `python main.py --mode test` (from project directory)
- Review `README.md` in the `extension/` folder for feature details

## Uninstalling

1. Go to `chrome://extensions/`
2. Find "AI Job Application Autofiller"
3. Click the trash icon to remove

Your settings are stored locally and will be removed when uninstalled.
