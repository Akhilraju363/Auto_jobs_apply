class PopupManager {
    constructor() {
        this.apiKeyInput = document.getElementById('api-key');
        this.resumeInput = document.getElementById('resume-text');
        this.jobTitlesInput = document.getElementById('job-titles');

        this.portfolioUrlInput = document.getElementById('portfolio-url');
        this.githubUrlInput = document.getElementById('github-url');
        this.linkedinUrlInput = document.getElementById('linkedin-url');
        this.coverLetterInput = document.getElementById('cover-letter-text');

        this.expectedSalaryInput = document.getElementById('expected-salary');
        this.noticePeriodInput = document.getElementById('notice-period');
        this.visaSponsorshipInput = document.getElementById('visa-sponsorship');

        this.apiStatusBadge = document.getElementById('api-status');
        this.resumeStatusBadge = document.getElementById('resume-status');
        this.titlesStatusBadge = document.getElementById('titles-status');
        this.portfolioStatusBadge = document.getElementById('portfolio-status');
        this.coverLetterStatusBadge = document.getElementById('cover-letter-status');
        this.preferencesStatusBadge = document.getElementById('preferences-status');

        this.resumePdfInput = document.getElementById('resume-pdf');
        this.pdfStatusBadge = document.getElementById('pdf-status');
        this.pdfInfoDiv = document.getElementById('pdf-info');
        this.pdfFilenameSpan = document.getElementById('pdf-filename');
        this.pdfSizeSpan = document.getElementById('pdf-size');
        this.pdfDateSpan = document.getElementById('pdf-date');
        this.clearPdfBtn = document.getElementById('clear-pdf-btn');

        this.saveBtn = document.getElementById('save-btn');
        this.resetBtn = document.getElementById('reset-btn');
        this.startAutofillBtn = document.getElementById('startAutofillBtn');
        this.autofillProgress = document.getElementById('autofillProgress');
        this.progressText = document.getElementById('progressText');
        this.progressDots = [
            document.getElementById('progressDot1'),
            document.getElementById('progressDot2'),
            document.getElementById('progressDot3'),
            document.getElementById('progressDot4'),
        ];

        this.toggleApiKeyBtn = document.getElementById('toggle-api-key');

        this.MAX_PDF_SIZE = 10 * 1024 * 1024; // 10MB limit

        this.initialize();
    }

    async initialize() {
        await this.loadSettings();
        this.attachEventListeners();
    }

    async loadSettings() {
        const data = await chrome.storage.local.get([
            'apiKey',
            'resumeText',
            'jobTitles',
            'resumePdfMetadata',
            'portfolioUrl',
            'githubUrl',
            'linkedinUrl',
            'coverLetterText',
            'expectedSalary',
            'noticePeriod',
            'visaSponsorship',
        ]);

        if (data.apiKey) {
            this.apiKeyInput.value = data.apiKey;
            this.updateStatusBadge(this.apiStatusBadge, true);
        }

        if (data.resumeText) {
            this.resumeInput.value = data.resumeText;
            this.updateStatusBadge(this.resumeStatusBadge, true);
        }

        if (data.jobTitles) {
            this.jobTitlesInput.value = data.jobTitles;
            this.updateStatusBadge(this.titlesStatusBadge, true);
        }

        if (data.portfolioUrl) {
            this.portfolioUrlInput.value = data.portfolioUrl;
            this.updateStatusBadge(this.portfolioStatusBadge, true);
        }

        if (data.githubUrl) {
            this.githubUrlInput.value = data.githubUrl;
            this.updateStatusBadge(this.portfolioStatusBadge, true);
        }

        if (data.linkedinUrl) {
            this.linkedinUrlInput.value = data.linkedinUrl;
            this.updateStatusBadge(this.portfolioStatusBadge, true);
        }

        if (data.coverLetterText) {
            this.coverLetterInput.value = data.coverLetterText;
            this.updateStatusBadge(this.coverLetterStatusBadge, true);
        }

        if (data.expectedSalary || data.noticePeriod || data.visaSponsorship) {
            this.updateStatusBadge(this.preferencesStatusBadge, true);
        }

        if (data.expectedSalary) {
            this.expectedSalaryInput.value = data.expectedSalary;
        }

        if (data.noticePeriod) {
            this.noticePeriodInput.value = data.noticePeriod;
        }

        if (data.visaSponsorship) {
            this.visaSponsorshipInput.value = data.visaSponsorship;
        }

        if (data.resumePdfMetadata) {
            this.displayPdfStatus(data.resumePdfMetadata);
        }
    }

    attachEventListeners() {
        this.saveBtn.addEventListener('click', () => this.saveSettings());
        this.resetBtn.addEventListener('click', () => this.resetSettings());
        this.startAutofillBtn.addEventListener('click', () => this.triggerAutofill());
        this.toggleApiKeyBtn.addEventListener('click', () =>
            this.togglePasswordVisibility()
        );

        this.resumePdfInput.addEventListener('change', (e) =>
            this.handlePdfUpload(e)
        );
        this.clearPdfBtn.addEventListener('click', () =>
            this.clearPdf()
        );

        this.apiKeyInput.addEventListener('input', () =>
            this.updateStatusBadge(this.apiStatusBadge, false)
        );
        this.resumeInput.addEventListener('input', () =>
            this.updateStatusBadge(this.resumeStatusBadge, false)
        );
        this.jobTitlesInput.addEventListener('input', () =>
            this.updateStatusBadge(this.titlesStatusBadge, false)
        );
        this.portfolioUrlInput.addEventListener('input', () =>
            this.updateStatusBadge(this.portfolioStatusBadge, false)
        );
        this.githubUrlInput.addEventListener('input', () =>
            this.updateStatusBadge(this.portfolioStatusBadge, false)
        );
        this.linkedinUrlInput.addEventListener('input', () =>
            this.updateStatusBadge(this.portfolioStatusBadge, false)
        );
        this.coverLetterInput.addEventListener('input', () =>
            this.updateStatusBadge(this.coverLetterStatusBadge, false)
        );
        this.expectedSalaryInput.addEventListener('input', () =>
            this.updateStatusBadge(this.preferencesStatusBadge, false)
        );
        this.noticePeriodInput.addEventListener('input', () =>
            this.updateStatusBadge(this.preferencesStatusBadge, false)
        );
        this.visaSponsorshipInput.addEventListener('input', () =>
            this.updateStatusBadge(this.preferencesStatusBadge, false)
        );
    }

    updateStatusBadge(badge, saved) {
        if (saved) {
            badge.textContent = '✓ Saved';
            badge.classList.remove('unsaved');
            badge.classList.add('saved');
        } else {
            badge.textContent = 'Unsaved';
            badge.classList.remove('saved');
            badge.classList.add('unsaved');
        }
    }

    async saveSettings() {
        const apiKey = this.apiKeyInput.value.trim();
        const resumeText = this.resumeInput.value.trim();
        const jobTitles = this.jobTitlesInput.value.trim();
        const portfolioUrl = this.portfolioUrlInput.value.trim();
        const githubUrl = this.githubUrlInput.value.trim();
        const linkedinUrl = this.linkedinUrlInput.value.trim();
        const coverLetterText = this.coverLetterInput.value.trim();
        const expectedSalary = this.expectedSalaryInput.value.trim();
        const noticePeriod = this.noticePeriodInput.value.trim();
        const visaSponsorship = this.visaSponsorshipInput.value.trim();

        if (!apiKey) {
            this.showAlert('⚠️ API Key is required', 'error');
            return;
        }

        if (apiKey.length < 20) {
            this.showAlert('⚠️ API Key looks incomplete (too short)', 'warning');
            return;
        }

        if (!resumeText) {
            this.showAlert('⚠️ Resume text is required', 'error');
            return;
        }

        if (resumeText.length < 50) {
            this.showAlert('⚠️ Resume is too short (minimum 50 chars)', 'warning');
            return;
        }

        // Validate URLs if provided
        if (portfolioUrl && !this.isValidUrl(portfolioUrl)) {
            this.showAlert('⚠️ Portfolio URL is invalid', 'warning');
            return;
        }

        if (githubUrl && !this.isValidUrl(githubUrl)) {
            this.showAlert('⚠️ GitHub URL is invalid', 'warning');
            return;
        }

        if (linkedinUrl && !this.isValidUrl(linkedinUrl)) {
            this.showAlert('⚠️ LinkedIn URL is invalid', 'warning');
            return;
        }

        try {
            this.saveBtn.disabled = true;
            this.saveBtn.textContent = '💾 Saving...';

            await chrome.storage.local.set({
                apiKey,
                resumeText,
                jobTitles: jobTitles || 'Software Engineer, Backend Developer, SDE',
                portfolioUrl: portfolioUrl || '',
                githubUrl: githubUrl || '',
                linkedinUrl: linkedinUrl || '',
                coverLetterText: coverLetterText || '',
                expectedSalary: expectedSalary || '',
                noticePeriod: noticePeriod || '',
                visaSponsorship: visaSponsorship || '',
            });

            this.updateStatusBadge(this.apiStatusBadge, true);
            this.updateStatusBadge(this.resumeStatusBadge, true);
            this.updateStatusBadge(this.titlesStatusBadge, true);
            this.updateStatusBadge(this.portfolioStatusBadge, true);
            this.updateStatusBadge(this.coverLetterStatusBadge, true);

            this.showAlert('✓ Settings saved successfully!', 'success');

            setTimeout(() => {
                this.saveBtn.disabled = false;
                this.saveBtn.textContent = '💾 Save Settings';
            }, 2000);
        } catch (error) {
            console.error('Save error:', error);
            this.showAlert(
                `❌ Save failed: ${error.message}`,
                'error'
            );

            this.saveBtn.disabled = false;
            this.saveBtn.textContent = '💾 Save Settings';
        }
    }

    resetSettings() {
        if (confirm('❌ Reset all settings? This cannot be undone.')) {
            this.apiKeyInput.value = '';
            this.resumeInput.value = '';
            this.jobTitlesInput.value = '';
            this.portfolioUrlInput.value = '';
            this.githubUrlInput.value = '';
            this.linkedinUrlInput.value = '';
            this.coverLetterInput.value = '';
            this.expectedSalaryInput.value = '';
            this.noticePeriodInput.value = '';
            this.visaSponsorshipInput.value = '';

            chrome.storage.local.remove([
                'apiKey',
                'resumeText',
                'jobTitles',
                'resumePdfData',
                'resumePdfMetadata',
                'portfolioUrl',
                'githubUrl',
                'linkedinUrl',
                'coverLetterText',
                'expectedSalary',
                'noticePeriod',
                'visaSponsorship',
            ]);

            this.updateStatusBadge(this.apiStatusBadge, false);
            this.updateStatusBadge(this.resumeStatusBadge, false);
            this.updateStatusBadge(this.titlesStatusBadge, false);
            this.updateStatusBadge(this.portfolioStatusBadge, false);
            this.updateStatusBadge(this.coverLetterStatusBadge, false);
            this.updateStatusBadge(this.preferencesStatusBadge, false);

            this.clearPdf();

            this.showAlert('↺ Settings reset to defaults', 'info');
        }
    }

    isValidUrl(url) {
        try {
            new URL(url);
            return true;
        } catch (e) {
            return false;
        }
    }

    async handlePdfUpload(event) {
        const file = event.target.files?.[0];
        if (!file) return;

        if (file.type !== 'application/pdf') {
            this.showAlert('⚠️ Please select a valid PDF file', 'warning');
            this.resumePdfInput.value = '';
            return;
        }

        if (file.size > this.MAX_PDF_SIZE) {
            this.showAlert(`⚠️ PDF is too large. Max size: 10MB`, 'error');
            this.resumePdfInput.value = '';
            return;
        }

        try {
            const reader = new FileReader();

            reader.onload = async (e) => {
                try {
                    const arrayBuffer = e.target.result;
                    const base64String = this.arrayBufferToBase64(arrayBuffer);

                    const metadata = {
                        filename: file.name,
                        size: file.size,
                        uploadedAt: new Date().toISOString(),
                    };

                    await chrome.storage.local.set({
                        resumePdfData: base64String,
                        resumePdfMetadata: metadata,
                    });

                    this.displayPdfStatus(metadata);
                    this.showAlert(`✓ PDF "${file.name}" uploaded successfully!`, 'success');
                } catch (error) {
                    console.error('PDF storage error:', error);
                    this.showAlert('❌ Failed to save PDF', 'error');
                }
            };

            reader.onerror = () => {
                this.showAlert('❌ Failed to read PDF file', 'error');
            };

            reader.readAsArrayBuffer(file);
        } catch (error) {
            console.error('PDF upload error:', error);
            this.showAlert('❌ Error uploading PDF', 'error');
        }
    }

    displayPdfStatus(metadata) {
        if (!metadata) return;

        const sizeInMB = (metadata.size / (1024 * 1024)).toFixed(2);
        const uploadDate = new Date(metadata.uploadedAt).toLocaleDateString();

        this.pdfFilenameSpan.textContent = metadata.filename;
        this.pdfSizeSpan.textContent = `Size: ${sizeInMB} MB`;
        this.pdfDateSpan.textContent = `Uploaded: ${uploadDate}`;

        this.pdfInfoDiv.style.display = 'block';
        this.clearPdfBtn.style.display = 'block';

        this.pdfStatusBadge.textContent = '✓ PDF Ready';
        this.pdfStatusBadge.classList.remove('unsaved');
        this.pdfStatusBadge.classList.add('saved');
    }

    async clearPdf() {
        if (confirm('Are you sure you want to remove the uploaded PDF?')) {
            try {
                await chrome.storage.local.remove(['resumePdfData', 'resumePdfMetadata']);

                this.resumePdfInput.value = '';
                this.pdfInfoDiv.style.display = 'none';
                this.clearPdfBtn.style.display = 'none';

                this.pdfStatusBadge.textContent = 'No PDF';
                this.pdfStatusBadge.classList.remove('saved');
                this.pdfStatusBadge.classList.add('unsaved');

                this.showAlert('PDF removed', 'info');
            } catch (error) {
                console.error('PDF removal error:', error);
                this.showAlert('❌ Failed to remove PDF', 'error');
            }
        }
    }

    arrayBufferToBase64(arrayBuffer) {
        const bytes = new Uint8Array(arrayBuffer);
        let binary = '';
        for (let i = 0; i < bytes.byteLength; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        return btoa(binary);
    }

    togglePasswordVisibility() {
        const isPassword = this.apiKeyInput.type === 'password';
        this.apiKeyInput.type = isPassword ? 'text' : 'password';
        this.toggleApiKeyBtn.textContent = isPassword ? 'Hide' : 'Show';
    }

    async triggerAutofill() {
        try {
            const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
            if (!tabs || tabs.length === 0) {
                this.showAlert('❌ No active tab found', 'error');
                return;
            }

            const activeTab = tabs[0];
            if (!activeTab.id) {
                this.showAlert('❌ Cannot access this page', 'error');
                return;
            }

            this.startAutofillBtn.disabled = true;
            this.startAutofillBtn.style.opacity = '0.6';
            this.autofillProgress.style.display = 'block';

            chrome.tabs.sendMessage(activeTab.id, { action: 'triggerAutofill' }, (response) => {
                if (chrome.runtime.lastError) {
                    this.showAlert('❌ Cannot access this page', 'error');
                } else if (response && response.success) {
                    this.showAlert('✓ Autofill completed!', 'success');
                } else {
                    this.showAlert('⚠️ Autofill finished with some fields', 'warning');
                }

                this.startAutofillBtn.disabled = false;
                this.startAutofillBtn.style.opacity = '1';
                this.autofillProgress.style.display = 'none';
            });
        } catch (error) {
            console.error('Autofill trigger error:', error);
            this.showAlert('❌ Error triggering autofill', 'error');
            this.startAutofillBtn.disabled = false;
            this.startAutofillBtn.style.opacity = '1';
            this.autofillProgress.style.display = 'none';
        }
    }

    updateProgress(step) {
        const steps = {
            'links': { text: '🔗 Filling Links & Files...', dot: 0 },
            'preferences': { text: '💼 Filling Preferences...', dot: 1 },
            'resume': { text: '📄 Filling Resume Fields...', dot: 2 },
            'questions': { text: '🤖 Processing Questions...', dot: 3 },
        };

        const config = steps[step];
        if (config) {
            this.progressText.textContent = config.text;
            this.progressDots.forEach((dot, idx) => {
                dot.style.backgroundColor = idx <= config.dot ? '#0066cc' : '#ddd';
            });
        }
    }

    showAlert(message, type = 'info') {
        const alert = document.createElement('div');

        const styles = {
            info: 'background-color: #3b82f6; color: white;',
            success: 'background-color: #22c55e; color: white;',
            warning: 'background-color: #f59e0b; color: white;',
            error: 'background-color: #ef4444; color: white;',
        };

        alert.style.cssText = `
            position: fixed;
            top: 8px;
            right: 8px;
            padding: 10px 14px;
            ${styles[type] || styles.info}
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            z-index: 9999;
            animation: slideIn 0.3s ease-out;
            max-width: 280px;
            word-wrap: break-word;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
        `;

        alert.textContent = message;
        document.body.appendChild(alert);

        setTimeout(() => {
            alert.style.animation = 'slideOut 0.3s ease-out';
            setTimeout(() => alert.remove(), 300);
        }, 4000);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        new PopupManager();
    });
} else {
    new PopupManager();
}
