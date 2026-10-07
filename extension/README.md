# AI Job Application Autofiller - Chrome Extension

A Manifest V3 Chrome Extension that uses Google's Gemini API to intelligently autofill job application forms across multiple platforms.

## Features

- **AI-Powered Form Filling**: Uses Gemini 2.5 Flash to generate contextual answers based on your resume
- **Multi-Platform Support**: Works on Workday, LinkedIn, Naukri, Greenhouse, Lever, and more
- **One-Click Configuration**: Simple popup UI for API key, resume, and job title management
- **Automatic Detection**: Identifies job application fields and adds fill buttons
- **Secure Storage**: Uses Chrome's local storage API (data stays on your machine)

## Installation

1. **Clone or download** this extension folder
2. Open `chrome://extensions/` in Chrome
3. Enable "Developer mode" (top-right toggle)
4. Click "Load unpacked" and select this folder
5. The extension icon will appear in your toolbar

## Configuration

1. Click the extension icon in your toolbar
2. **Add Gemini API Key**:
   - Get a free API key from [Google AI Studio](https://aistudio.google.com/app/apikey)
   - Paste it in the "API Key" field
3. **Add Your Resume**: Paste your resume or professional summary
4. **Target Job Titles**: (Optional) Add comma-separated job titles you're interested in
5. Click **"💾 Save Settings"**

## Usage

Once configured:

1. Navigate to any job application form (Workday, LinkedIn, etc.)
2. The extension automatically detects form fields
3. Look for **"✨ AI Fill"** buttons next to text fields
4. Click a button to generate and fill the answer using your resume
5. Review and edit the generated text if needed

## File Structure

```
chrome_extension/
├── manifest.json          # Extension configuration (MV3)
├── popup.html            # Settings UI
├── popup.js              # Settings logic
├── content.js            # Form detection & autofill
├── gemini.js             # Gemini API client
├── background.js         # Service worker
├── .env.example          # Configuration template
└── resume.txt.example    # Resume template
```

## Security

- **No data collection**: All data is stored locally in Chrome
- **No tracking**: Extension does not track your activity
- **API key never shared**: Requests go directly to Google's API
- **Resume stays private**: Never sent anywhere except to Gemini API for context

## Troubleshooting

**"Configure API Key and Resume" error**:
- Click the extension icon and fill in all required fields
- Make sure to click "Save Settings"

**Button doesn't appear on forms**:
- Some platforms may have different form structures
- Refresh the page or reload the extension
- Check that the field contains job-related keywords

**API errors**:
- Verify your API key is valid at [Google AI Studio](https://aistudio.google.com/app/apikey)
- Check you have API quota remaining
- Ensure CORS is not blocked (shouldn't be for official APIs)

## Supported Platforms

- Workday
- LinkedIn Jobs
- Naukri.com
- Greenhouse
- Lever
- Most standard web forms

## Future Enhancements

- [ ] Support for file uploads and cover letters
- [ ] Form pre-population from browser autofill
- [ ] Multiple resume profiles
- [ ] Application history tracking
- [ ] Advanced form field mapping

## License

MIT

## Support

For issues or feature requests, see the main project repository.
