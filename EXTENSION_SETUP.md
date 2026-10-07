# Chrome Extension Setup - Quick Reference

## TL;DR

1. **Copy this exact path**:
   ```
   C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply\extension
   ```

2. **In Chrome**:
   - Go to `chrome://extensions/`
   - Enable "Developer mode" (top-right)
   - Click "Load unpacked"
   - Paste the path above and select the folder
   - Click "Select Folder"

3. **Configure**:
   - Click extension icon in toolbar
   - Add API key from https://aistudio.google.com/app/apikey
   - Add your resume text
   - Click "Save Settings"

4. **Use**:
   - Go to any job application site
   - Click "✨" buttons to fill fields or "⚡ Fill All" to fill entire form

---

## Detailed Instructions

### Verify Extension is Ready

Run diagnostic check:
```bash
python main.py --mode test
```

Look for this line:
```
[+] Chrome Extension  [+] Ready. Load: C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply\extension
```

### Load into Chrome

**Step-by-step**:

1. Open Chrome
2. Type `chrome://extensions/` in address bar and press Enter
3. In top-right corner, toggle "Developer mode" ON (it turns blue)
4. Click "Load unpacked" button
5. **Important**: Navigate to the `extension` folder
   - You should see these files: `manifest.json`, `popup.html`, `content.js`, etc.
   - If you see folders like `chrome_extension` or `Auto_job_apply`, go back up
6. Click "Select Folder"

**Expected result**:
- Extension appears in your list as "AI Job Application Autofiller"
- Extension icon appears in your toolbar

### Configure Extension

1. Click the extension icon (small square in toolbar)
2. Three sections will appear:

   **Gemini API Configuration**:
   - Get API key from: https://aistudio.google.com/app/apikey
   - Paste into "API Key" field
   - Click the eye icon to verify it's pasted correctly

   **Resume Text**:
   - Paste your complete resume or professional summary
   - Minimum 50 characters

   **Target Job Titles** (optional):
   - List job titles you're interested in (comma-separated)
   - Example: `Software Engineer, Backend Developer, SDE`

3. Click "💾 Save Settings"
4. You should see ✓ Saved next to each section

### Use the Extension

**On any job application site**:
- Individual field buttons: Click "✨" next to a text field to fill just that field
- Bulk fill: Click "⚡ Fill All" (bottom-right corner) to fill entire form

**Status messages appear** in bottom-right corner:
- "Analyzing Form..." → "Filling field 2 of 5..." → "✓ Form Autofilled Successfully!"

---

## Directory Structure

```
Auto_job_apply/
├── extension/                    ← Load this folder in Chrome
│   ├── manifest.json            (required)
│   ├── popup.html               (required)
│   ├── popup.js                 (required)
│   ├── content.js               (required)
│   ├── gemini.js                (required)
│   ├── background.js            (required)
│   ├── .env.example
│   ├── resume.txt.example
│   ├── README.md
│   └── INSTALL.md
├── main.py
├── requirements.txt
├── EXTENSION_SETUP.md            (this file)
└── ... (other project files)
```

---

## Common Issues & Solutions

### "Manifest file is missing or unreadable"

**Wrong**: Selected `C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply\`

**Correct**: Select `C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\Auto_job_apply\extension\`

When you navigate to the folder, you should see these files directly:
- `manifest.json` ✓
- `popup.html` ✓
- `content.js` ✓
- etc.

### Extension doesn't appear after loading

1. Refresh the extensions page: F5
2. Verify Developer mode is still ON (blue toggle)
3. Check error message in red under extension name
4. Click "Details" to see console errors

### "API Key not found" on job sites

1. Click extension icon
2. Verify API key is visible in the field
3. Click "Save Settings" again
4. Refresh the job site page

### Extension icon not visible in toolbar

1. Go to `chrome://extensions/`
2. Find "AI Job Application Autofiller"
3. Click the pin icon to add to toolbar
4. Icon should appear next to address bar

---

## Verify Everything Works

**Test the installation**:

1. Go to any website with a text input form
2. Click the extension icon
3. You should see the popup open
4. Verify your API key and resume are still saved
5. Close popup (click outside it)
6. Find a text input on the page
7. You should see a "✨" button appear next to the input
8. Click it - it should show loading, then fill the field

---

## Reset/Reinstall

If something goes wrong:

1. Go to `chrome://extensions/`
2. Find "AI Job Application Autofiller"
3. Click the trash icon to remove
4. Refresh this page
5. Extension is uninstalled
6. Follow "Load into Chrome" steps again

---

## Support

**For diagnostic info**, run:
```bash
python main.py --mode test
```

This will show you the exact path to load and verify all components are present.

**For more details**, see:
- `extension/INSTALL.md` - Full installation guide
- `extension/README.md` - Feature documentation
- Project README for full automation engine info
