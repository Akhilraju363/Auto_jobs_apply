class ResumeParser {
    constructor(resumeText) {
        this.resumeText = resumeText || '';
        this.extractedData = this.parseResume();
    }

    parseResume() {
        return {
            email: this.extractEmail(),
            phone: this.extractPhone(),
            name: this.extractName(),
            location: this.extractLocation(),
            currentJobTitle: this.extractCurrentJobTitle(),
            yearsOfExperience: this.extractYearsOfExperience(),
            skills: this.extractSkills(),
            education: this.extractEducation(),
            companies: this.extractCompanies(),
        };
    }

    extractEmail() {
        const emailRegex = /([a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+\.[a-zA-Z0-9_-]+)/;
        const match = this.resumeText.match(emailRegex);
        return match ? match[1] : '';
    }

    extractPhone() {
        const phoneRegex = /(\+?1?\s?)?(\([0-9]{3}\)|[0-9]{3})[\s.-]?[0-9]{3}[\s.-]?[0-9]{4}/;
        const match = this.resumeText.match(phoneRegex);
        return match ? match[0].trim() : '';
    }

    extractName() {
        const lines = this.resumeText.split('\n');
        for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.length > 0 && trimmed.length < 100 && /^[A-Z][a-z]+\s[A-Z]/.test(trimmed)) {
                return trimmed;
            }
        }
        return '';
    }

    extractLocation() {
        const locationRegex = /(?:Location|City|Based in|from)[\s:]+([A-Za-z\s,]+?)(?:\n|$)/i;
        const match = this.resumeText.match(locationRegex);
        return match ? match[1].trim() : '';
    }

    extractCurrentJobTitle() {
        const titleRegex = /(?:Current\s+Title|Position|Role)[\s:]+([^\n]+)/i;
        const match = this.resumeText.match(titleRegex);
        if (match) return match[1].trim();

        const lines = this.resumeText.split('\n');
        for (let i = 0; i < lines.length; i++) {
            if (/^[A-Z][a-z\s]+(?:Engineer|Developer|Manager|Lead|Architect|Designer|Director|Analyst|Specialist)/i.test(lines[i].trim())) {
                return lines[i].trim();
            }
        }
        return '';
    }

    extractYearsOfExperience() {
        const expRegex = /(\d+)\+?\s+years?\s+of\s+(?:professional\s+)?experience/i;
        const match = this.resumeText.match(expRegex);
        return match ? match[1] : '';
    }

    extractSkills() {
        const skillsRegex = /(?:Skills|Technical Skills|Proficiencies?)[\s:]+([^]*?)(?=\n\n|EXPERIENCE|EDUCATION|$)/i;
        const match = this.resumeText.match(skillsRegex);
        if (match) {
            return match[1]
                .split(/[,\n]/)
                .map(s => s.trim())
                .filter(s => s.length > 0)
                .slice(0, 20)
                .join(', ');
        }
        return '';
    }

    extractEducation() {
        const eduRegex = /(?:B\.?[A-Z]\.?|M\.?[A-Z]\.?|Ph\.?D\.?|Bachelor|Master|Degree|Diploma)\s+(?:in\s+)?([^\n,]+)/i;
        const match = this.resumeText.match(eduRegex);
        return match ? match[0].trim() : '';
    }

    extractCompanies() {
        const companyLines = this.resumeText.split('\n').filter(line => {
            const trimmed = line.trim();
            return trimmed.length > 3 && trimmed.length < 80 && /[A-Z]/.test(trimmed);
        });
        return companyLines.slice(0, 10).map(c => c.trim()).join(', ');
    }
}

class UniversalATSFormEngine {
    constructor() {
        this.apiKey = null;
        this.resumeText = null;
        this.userProfile = {};
        this.gemini = null;
        this.isProcessing = false;
        this.processedForms = new WeakSet();
        this.processedFileInputs = new WeakSet();
        this.toastContainer = null;
        this.resumePdfData = null;
        this.resumePdfMetadata = null;
        this.resumeParser = null;

        this.initialize();
    }

    async initialize() {
        this.initializeToastContainer();
        await this.loadSettings();
        if (this.resumeText) {
            this.resumeParser = new ResumeParser(this.resumeText);
        }
        this.validateConfiguration();
        this.scanAndProcessForms();
        this.scanAndAttachResumePdf();
        this.scanAndFillPortfolioLinks();
        this.scanAndFillCoverLetters();
        this.scanAndFillPreferences();
        this.scanAndFillStandardFields();
        this.setupSPANavigation();
    }

    initializeToastContainer() {
        this.toastContainer = document.createElement('div');
        this.toastContainer.id = 'ai-toast-container';
        this.toastContainer.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            z-index: 999999;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        `;
        document.body.appendChild(this.toastContainer);
    }

    async loadSettings() {
        const data = await chrome.storage.local.get([
            'apiKey',
            'resumeText',
            'userProfile',
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

        this.apiKey = data.apiKey;
        this.resumeText = data.resumeText;
        this.userProfile = data.userProfile || {};
        this.resumePdfData = data.resumePdfData;
        this.resumePdfMetadata = data.resumePdfMetadata;
        this.portfolioUrl = data.portfolioUrl;
        this.githubUrl = data.githubUrl;
        this.linkedinUrl = data.linkedinUrl;
        this.coverLetterText = data.coverLetterText;
        this.expectedSalary = data.expectedSalary;
        this.noticePeriod = data.noticePeriod;
        this.visaSponsorship = data.visaSponsorship;

        if (this.apiKey) {
            this.gemini = new GeminiClient(this.apiKey);
        }
    }

    validateConfiguration() {
        if (!this.apiKey) {
            this.showStatusToast(
                '⚠️ Gemini API key not found. Click the JobFlow extension icon to configure.',
                'error',
                6000
            );
            return false;
        }

        if (!this.resumeText) {
            this.showStatusToast(
                '⚠️ Resume not configured. Update in the extension popup.',
                'warning',
                6000
            );
            return false;
        }

        return true;
    }

    showStatusToast(message, type = 'info', duration = 4000) {
        try {
            const toast = document.createElement('div');
            const icons = {
                info: 'ℹ️',
                success: '✓',
                error: '❌',
                warning: '⚠️',
                loading: '⏳',
            };

            toast.className = `ai-toast ai-toast-${type}`;
            toast.innerHTML = `${icons[type] || type} ${message}`;

            const baseStyles = `
                display: inline-block;
                padding: 12px 16px;
                margin-bottom: 8px;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 255, 255, 0.2);
                color: white;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
                animation: slideIn 0.3s ease-out;
                word-wrap: break-word;
                max-width: 300px;
            `;

            const typeStyles = {
                info: 'background: rgba(59, 130, 246, 0.8); border-color: rgba(59, 130, 246, 0.3);',
                success: 'background: rgba(34, 197, 94, 0.8); border-color: rgba(34, 197, 94, 0.3);',
                error: 'background: rgba(239, 68, 68, 0.8); border-color: rgba(239, 68, 68, 0.3);',
                warning: 'background: rgba(245, 158, 11, 0.8); border-color: rgba(245, 158, 11, 0.3);',
                loading: 'background: rgba(168, 85, 247, 0.8); border-color: rgba(168, 85, 247, 0.3);',
            };

            toast.style.cssText = baseStyles + (typeStyles[type] || typeStyles.info);

            if (!this.toastContainer) {
                this.initializeToastContainer();
            }

            this.toastContainer.appendChild(toast);

            if (duration > 0) {
                setTimeout(() => {
                    toast.style.animation = 'slideOut 0.3s ease-out';
                    setTimeout(() => toast.remove(), 300);
                }, duration);
            }

            this.ensureAnimationStyles();
        } catch (e) {
            console.error('Toast error:', e);
        }
    }

    ensureAnimationStyles() {
        if (!document.getElementById('ai-toast-styles')) {
            const style = document.createElement('style');
            style.id = 'ai-toast-styles';
            style.textContent = `
                @keyframes slideIn {
                    from {
                        transform: translateX(400px);
                        opacity: 0;
                    }
                    to {
                        transform: translateX(0);
                        opacity: 1;
                    }
                }
                @keyframes slideOut {
                    from {
                        transform: translateX(0);
                        opacity: 1;
                    }
                    to {
                        transform: translateX(400px);
                        opacity: 0;
                    }
                }
            `;
            document.head.appendChild(style);
        }
    }

    setupSPANavigation() {
        let lastUrl = location.href;

        const urlObserver = setInterval(() => {
            const currentUrl = location.href;
            if (currentUrl !== lastUrl) {
                lastUrl = currentUrl;
                setTimeout(() => {
                    this.scanAndProcessForms();
                    this.scanAndAttachResumePdf();
                    this.scanAndFillPortfolioLinks();
                    this.scanAndFillCoverLetters();
                    this.scanAndFillPreferences();
                    this.scanAndFillStandardFields();
                }, 500);
            }
        }, 1000);

        const mutationObserver = new MutationObserver(() => {
            if (Math.random() < 0.05) {
                this.scanAndProcessForms();
                this.scanAndAttachResumePdf();
                this.scanAndFillPortfolioLinks();
                this.scanAndFillCoverLetters();
                this.scanAndFillPreferences();
                this.scanAndFillStandardFields();
            }
        });

        try {
            mutationObserver.observe(document.body, {
                childList: true,
                subtree: true,
            });
        } catch (e) {
            console.warn('Mutation observer setup failed:', e);
        }
    }

    scanAndProcessForms() {
        try {
            const formSelectors = [
                'form',
                '[role="form"]',
                '[data-form]',
                '.form-container',
                '[class*="form"]',
            ];

            const forms = document.querySelectorAll(formSelectors.join(','));

            forms.forEach((form) => {
                if (!this.processedForms.has(form)) {
                    this.processForm(form);
                    this.processedForms.add(form);
                }
            });
        } catch (e) {
            console.error('Form scanning error:', e);
        }
    }

    processForm(form) {
        const fields = this.detectFormFields(form);

        if (fields.length === 0) return;

        fields.forEach((field) => {
            this.injectFieldButton(field.element, field.label);
        });

        this.injectFormLevelButton(form, fields);
    }

    detectFormFields(form) {
        const fields = [];

        try {
            const inputs = form.querySelectorAll(
                'input[type="text"], input[type="email"], input[type="tel"], input[type="number"], textarea, [contenteditable="true"]'
            );

            inputs.forEach((input) => {
                if (this.isFieldFilled(input)) return;

                const label = this.getLabelTextForElement(input);
                if (label && this.isJobApplicationField(label)) {
                    fields.push({ element: input, label });
                }
            });
        } catch (e) {
            console.error('Field detection error:', e);
        }

        return fields;
    }

    getLabelTextForElement(element) {
        try {
            if (element.id) {
                const label = document.querySelector(`label[for="${element.id}"]`);
                if (label?.textContent) {
                    return label.textContent.trim();
                }
            }

            const parentLabel = element.closest('label');
            if (parentLabel?.textContent) {
                return parentLabel.textContent.trim();
            }

            if (element.getAttribute('aria-label')) {
                return element.getAttribute('aria-label').trim();
            }

            if (element.placeholder) {
                return element.placeholder.trim();
            }

            if (element.title) {
                return element.title.trim();
            }

            let prev = element.previousElementSibling;
            while (prev && prev !== element) {
                if (prev.tagName === 'LABEL' && prev.textContent) {
                    return prev.textContent.trim();
                }
                if (prev.textContent && prev.textContent.trim()) {
                    return prev.textContent.trim().substring(0, 150);
                }
                prev = prev.previousElementSibling;
            }

            const parent = element.parentElement;
            if (parent) {
                const labels = parent.querySelectorAll('label, .label, [class*="label"]');
                if (labels.length > 0 && labels[0].textContent) {
                    return labels[0].textContent.trim();
                }
            }

            if (element.name) return element.name.trim();
            if (element.id) return element.id.trim();

            return '';
        } catch (e) {
            console.warn('Label extraction error:', e);
            return element.name || element.id || '';
        }
    }

    isFieldFilled(element) {
        try {
            if (element.tagName === 'TEXTAREA') {
                return element.value?.trim().length > 0;
            }
            if (element.contentEditable === 'true') {
                return element.textContent?.trim().length > 0;
            }
            return element.value?.trim().length > 0;
        } catch (e) {
            return false;
        }
    }

    isJobApplicationField(label) {
        const keywords = [
            'name',
            'email',
            'phone',
            'experience',
            'skills',
            'motivation',
            'interest',
            'background',
            'question',
            'why',
            'tell',
            'describe',
            'cover',
            'letter',
            'additional',
            'comment',
            'years',
            'notice',
            'salary',
            'ctc',
            'expected',
        ];

        const lowerLabel = label.toLowerCase();
        return keywords.some((kw) => lowerLabel.includes(kw));
    }

    fillInputValue(element, value) {
        try {
            if (!element || !value) return;

            const descriptor = Object.getOwnPropertyDescriptor(
                Object.getPrototypeOf(element),
                'value'
            );

            if (descriptor?.set) {
                descriptor.set.call(element, value);
            } else {
                element.value = value;
            }

            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
            element.dispatchEvent(new Event('blur', { bubbles: true }));

            if (element.contentEditable === 'true') {
                element.textContent = value;
                element.dispatchEvent(new Event('input', { bubbles: true }));
            }
        } catch (e) {
            console.error('Fill error:', e);
            try {
                element.value = value;
            } catch (fallbackError) {
                console.warn('Fallback fill failed:', fallbackError);
            }
        }
    }

    async handleStandardFields(label) {
        const standardMap = {
            email: () => this.userProfile.email,
            phone: () => this.userProfile.phone,
            'first name': () => this.userProfile.firstName,
            'last name': () => this.userProfile.lastName,
            'years of experience': () =>
                this.userProfile.yearsExperience?.toString(),
            'notice period': () => this.userProfile.noticePeriod,
            ctc: () => this.userProfile.ctc,
            'expected salary': () => this.userProfile.expectedSalary,
        };

        const lowerLabel = label.toLowerCase();
        for (const [key, getter] of Object.entries(standardMap)) {
            if (lowerLabel.includes(key)) {
                const value = getter();
                if (value) return value;
            }
        }

        return null;
    }

    async generateAnswer(label) {
        const standard = await this.handleStandardFields(label);
        if (standard) return standard;

        if (!this.gemini || !this.resumeText) {
            throw new Error('API or resume not configured');
        }

        return await this.gemini.generateAnswer(label, this.resumeText);
    }

    injectFieldButton(element, label) {
        try {
            const container = element.parentElement;
            if (!container) return;

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'ai-field-btn';
            btn.textContent = '✨';
            btn.title = 'AI Fill this field';

            btn.style.cssText = `
                margin-left: 4px;
                padding: 4px 6px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 3px;
                font-size: 12px;
                cursor: pointer;
                transition: all 0.2s;
                white-space: nowrap;
            `;

            btn.onmouseover = () => (btn.style.transform = 'scale(1.05)');
            btn.onmouseout = () => (btn.style.transform = 'scale(1)');

            btn.onclick = async (e) => {
                e.preventDefault();
                await this.fillField(element, label, btn);
            };

            container.insertBefore(btn, element.nextSibling);
        } catch (e) {
            console.warn('Field button injection failed:', e);
        }
    }

    async fillField(element, label, button) {
        if (!this.validateConfiguration()) return;

        const originalText = button.textContent;
        button.disabled = true;

        try {
            button.textContent = '⏳';
            this.showStatusToast(`Filling: ${label.substring(0, 30)}...`, 'loading', 0);

            const answer = await this.generateAnswer(label);
            this.fillInputValue(element, answer);
            button.textContent = '✓';
            this.showStatusToast(`✓ Filled: ${label.substring(0, 30)}...`, 'success', 3000);

            setTimeout(() => {
                button.textContent = originalText;
                button.disabled = false;
            }, 1500);
        } catch (error) {
            console.error('Fill error:', error);
            button.textContent = '❌';

            const errorMsg = error.message.includes('API')
                ? '⚠️ API Error: Rate limited or network issue'
                : '⚠️ Failed to generate answer';

            this.showStatusToast(errorMsg, 'error', 5000);

            setTimeout(() => {
                button.textContent = originalText;
                button.disabled = false;
            }, 2000);
        }
    }

    injectFormLevelButton(form, fields) {
        try {
            if (document.querySelector('.ai-form-fab')) return;

            const fab = document.createElement('div');
            fab.className = 'ai-form-fab';

            fab.style.cssText = `
                position: fixed;
                bottom: 24px;
                right: 24px;
                z-index: 9999;
            `;

            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = '⚡ Fill All';
            button.className = 'ai-fab-btn';

            button.style.cssText = `
                padding: 12px 18px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 24px;
                font-weight: 600;
                font-size: 14px;
                cursor: pointer;
                box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
                transition: all 0.3s;
            `;

            button.onmouseover = () => {
                button.style.transform = 'translateY(-2px)';
                button.style.boxShadow = '0 6px 16px rgba(102, 126, 234, 0.6)';
            };

            button.onmouseout = () => {
                button.style.transform = 'translateY(0)';
                button.style.boxShadow = '0 4px 12px rgba(102, 126, 234, 0.4)';
            };

            button.onclick = async () => {
                await this.fillAllFields(fields, button);
            };

            fab.appendChild(button);
            document.body.appendChild(fab);
        } catch (e) {
            console.warn('FAB injection failed:', e);
        }
    }

    async fillAllFields(fields, button) {
        if (this.isProcessing) return;

        if (!this.validateConfiguration()) {
            this.showStatusToast(
                '⚠️ Cannot fill: Missing API key or resume',
                'error',
                5000
            );
            return;
        }

        const originalText = button.textContent;
        this.isProcessing = true;
        let filled = 0;
        let failed = 0;

        this.showStatusToast(
            `Starting form autofill (${fields.length} fields)...`,
            'loading',
            0
        );

        for (let i = 0; i < fields.length; i++) {
            const field = fields[i];

            try {
                if (this.isFieldFilled(field.element)) {
                    this.showStatusToast(
                        `Analyzing Form... ${i + 1}/${fields.length}`,
                        'loading',
                        0
                    );
                    continue;
                }

                this.showStatusToast(
                    `Filling field ${i + 1} of ${fields.length}...`,
                    'loading',
                    0
                );

                const answer = await this.generateAnswer(field.label);
                this.fillInputValue(field.element, answer);
                filled++;

                button.textContent = `⚡ ${filled}/${fields.length}`;

                await new Promise((resolve) => setTimeout(resolve, 400));
            } catch (error) {
                console.error(`Error filling "${field.label}":`, error);
                failed++;

                if (error.message.includes('429') || error.message.includes('rate')) {
                    this.showStatusToast(
                        '⚠️ Rate limited. Please wait before trying again.',
                        'warning',
                        4000
                    );
                    break;
                }
            }
        }

        this.showStatusToast(
            `✓ Form Autofilled Successfully! (${filled}/${fields.length} filled)`,
            'success',
            4000
        );

        button.textContent = '✓ Done';
        setTimeout(() => {
            button.textContent = originalText;
            this.isProcessing = false;
        }, 2000);
    }

    scanAndAttachResumePdf() {
        if (!this.resumePdfData) {
            return;
        }

        try {
            const fileInputs = document.querySelectorAll('input[type="file"]');

            fileInputs.forEach((input) => {
                if (!this.processedFileInputs.has(input)) {
                    this.processFileInput(input);
                    this.processedFileInputs.add(input);
                }
            });
        } catch (e) {
            console.error('Resume PDF scan error:', e);
        }
    }

    processFileInput(input) {
        try {
            const label = this.getFileInputLabel(input);
            const isResumeField = this.isResumeFileInput(label, input);

            if (!isResumeField) {
                return;
            }

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = '📄 Attach PDF';
            btn.title = 'Attach uploaded resume PDF';
            btn.className = 'ai-pdf-attach-btn';

            btn.style.cssText = `
                margin-left: 4px;
                padding: 4px 8px;
                background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                color: white;
                border: none;
                border-radius: 3px;
                font-size: 12px;
                font-weight: 500;
                cursor: pointer;
                transition: all 0.2s;
                white-space: nowrap;
            `;

            btn.onmouseover = () => (btn.style.transform = 'scale(1.05)');
            btn.onmouseout = () => (btn.style.transform = 'scale(1)');

            btn.onclick = async (e) => {
                e.preventDefault();
                await this.attachResumePdf(input, btn);
            };

            const container = input.parentElement;
            if (container) {
                container.insertBefore(btn, input.nextSibling);
            }
        } catch (e) {
            console.warn('File input processing error:', e);
        }
    }

    getFileInputLabel(input) {
        try {
            if (input.id) {
                const label = document.querySelector(`label[for="${input.id}"]`);
                if (label?.textContent) {
                    return label.textContent.trim();
                }
            }

            const parentLabel = input.closest('label');
            if (parentLabel?.textContent) {
                return parentLabel.textContent.trim();
            }

            if (input.getAttribute('aria-label')) {
                return input.getAttribute('aria-label').trim();
            }

            if (input.title) {
                return input.title.trim();
            }

            if (input.name) {
                return input.name.trim();
            }

            const parent = input.parentElement;
            if (parent) {
                const labels = parent.querySelectorAll('label');
                if (labels.length > 0 && labels[0].textContent) {
                    return labels[0].textContent.trim();
                }
            }

            return '';
        } catch (e) {
            return input.name || input.id || '';
        }
    }

    isResumeFileInput(label, input) {
        const resumeKeywords = [
            'resume',
            'cv',
            'curriculum',
            'vitae',
            'attach',
            'upload',
            'document',
            'pdf',
            'file',
        ];

        const lowerLabel = label.toLowerCase();
        return resumeKeywords.some((kw) => lowerLabel.includes(kw));
    }

    async attachResumePdf(fileInput, button) {
        const originalText = button.textContent;

        try {
            button.disabled = true;
            button.textContent = '⏳ Attaching...';

            const file = this.base64ToFile(
                this.resumePdfData,
                this.resumePdfMetadata.filename
            );

            const dt = new DataTransfer();
            dt.items.add(file);
            fileInput.files = dt.files;

            fileInput.dispatchEvent(new Event('input', { bubbles: true }));
            fileInput.dispatchEvent(new Event('change', { bubbles: true }));
            fileInput.dispatchEvent(new Event('blur', { bubbles: true }));

            button.textContent = '✓';
            this.showStatusToast(
                `✓ Resume PDF attached: ${this.resumePdfMetadata.filename}`,
                'success',
                3000
            );

            setTimeout(() => {
                button.textContent = originalText;
                button.disabled = false;
            }, 1500);
        } catch (error) {
            console.error('PDF attachment error:', error);
            button.textContent = '❌ Error';
            this.showStatusToast(
                '❌ Failed to attach resume PDF',
                'error',
                4000
            );

            setTimeout(() => {
                button.textContent = originalText;
                button.disabled = false;
            }, 2000);
        }
    }

    base64ToFile(base64String, filename) {
        try {
            const binaryString = atob(base64String);
            const bytes = new Uint8Array(binaryString.length);

            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }

            return new File([bytes], filename, { type: 'application/pdf' });
        } catch (error) {
            console.error('Base64 to File conversion error:', error);
            throw new Error('Failed to process resume PDF');
        }
    }

    scanAndFillPortfolioLinks() {
        if (!this.portfolioUrl && !this.githubUrl && !this.linkedinUrl) {
            return;
        }

        try {
            const inputs = document.querySelectorAll(
                'input[type="url"], input[type="text"]'
            );

            let portfolioFilled = false;
            let githubFilled = false;
            let linkedinFilled = false;

            inputs.forEach((input) => {
                if (!input.value) {
                    const label = this.getLabelTextForElement(input);
                    const isPortfolioField = this.isPortfolioField(label, input);
                    const isGithubField = this.isGithubField(label, input);
                    const isLinkedinField = this.isLinkedinField(label, input);

                    if (isPortfolioField && this.portfolioUrl && !portfolioFilled) {
                        this.fillInputValue(input, this.portfolioUrl);
                        portfolioFilled = true;
                    } else if (isGithubField && this.githubUrl && !githubFilled) {
                        this.fillInputValue(input, this.githubUrl);
                        githubFilled = true;
                    } else if (isLinkedinField && this.linkedinUrl && !linkedinFilled) {
                        this.fillInputValue(input, this.linkedinUrl);
                        linkedinFilled = true;
                    }
                }
            });
        } catch (e) {
            console.error('Portfolio link scan error:', e);
        }
    }

    scanAndFillCoverLetters() {
        if (!this.coverLetterText) {
            return;
        }

        try {
            const inputs = document.querySelectorAll(
                'textarea, input[type="text"], [contenteditable="true"]'
            );

            inputs.forEach((input) => {
                if (!this.isFieldFilled(input)) {
                    const label = this.getLabelTextForElement(input);
                    if (this.isCoverLetterField(label, input)) {
                        this.fillInputValue(input, this.coverLetterText);
                        this.showStatusToast(
                            '✓ Cover letter filled automatically',
                            'success',
                            3000
                        );
                    }
                }
            });
        } catch (e) {
            console.error('Cover letter scan error:', e);
        }
    }

    isPortfolioField(label, input) {
        const portfolioKeywords = [
            'portfolio',
            'personal website',
            'personal site',
            'website',
            'web presence',
            'online presence',
            'link',
            'links',
            'url',
            'web',
            'blog',
        ];

        const lowerLabel = label.toLowerCase();
        const id = (input.id || '').toLowerCase();
        const name = (input.name || '').toLowerCase();
        const placeholder = (input.placeholder || '').toLowerCase();

        return (
            portfolioKeywords.some(
                (kw) =>
                    lowerLabel.includes(kw) ||
                    id.includes(kw) ||
                    name.includes(kw) ||
                    placeholder.includes(kw)
            ) && !lowerLabel.includes('github') && !lowerLabel.includes('linkedin')
        );
    }

    isGithubField(label, input) {
        const githubKeywords = [
            'github',
            'work samples',
            'code samples',
            'sample code',
            'projects',
            'code repository',
            'source code',
        ];

        const lowerLabel = label.toLowerCase();
        const id = (input.id || '').toLowerCase();
        const name = (input.name || '').toLowerCase();
        const placeholder = (input.placeholder || '').toLowerCase();

        return githubKeywords.some(
            (kw) =>
                lowerLabel.includes(kw) ||
                id.includes(kw) ||
                name.includes(kw) ||
                placeholder.includes(kw)
        );
    }

    isLinkedinField(label, input) {
        const linkedinKeywords = [
            'linkedin',
            'professional profile',
            'social profile',
            'social media',
            'professional network',
            'profile url',
            'linkedin profile',
            'linkedin url',
        ];

        const lowerLabel = label.toLowerCase();
        const id = (input.id || '').toLowerCase();
        const name = (input.name || '').toLowerCase();
        const placeholder = (input.placeholder || '').toLowerCase();

        return linkedinKeywords.some(
            (kw) =>
                lowerLabel.includes(kw) ||
                id.includes(kw) ||
                name.includes(kw) ||
                placeholder.includes(kw)
        );
    }

    isCoverLetterField(label, input) {
        const coverLetterKeywords = [
            'cover letter',
            'cover',
            'letter',
            'additional information',
            'additional details',
            'additional comments',
            'comments',
            'message',
            'motivation',
            'why are you interested',
            'why do you want',
            'tell us about yourself',
            'describe yourself',
            'about you',
            'optional',
        ];

        const lowerLabel = label.toLowerCase();
        const id = (input.id || '').toLowerCase();
        const name = (input.name || '').toLowerCase();
        const placeholder = (input.placeholder || '').toLowerCase();

        return coverLetterKeywords.some(
            (kw) =>
                lowerLabel.includes(kw) ||
                id.includes(kw) ||
                name.includes(kw) ||
                placeholder.includes(kw)
        );
    }

    scanAndFillPreferences() {
        if (!this.expectedSalary && !this.noticePeriod && !this.visaSponsorship) {
            return;
        }

        try {
            const inputs = document.querySelectorAll(
                'input[type="text"], textarea, select'
            );

            inputs.forEach((input) => {
                if (!input.value) {
                    const label = this.getLabelTextForElement(input);

                    if (this.expectedSalary && this.isSalaryField(label, input)) {
                        this.fillInputValue(input, this.expectedSalary);
                    } else if (this.noticePeriod && this.isNoticePeriodField(label, input)) {
                        this.fillInputValue(input, this.noticePeriod);
                    } else if (this.visaSponsorship && this.isVisaSponsorshipField(label, input)) {
                        this.fillInputValue(input, this.visaSponsorship);
                    }
                }
            });
        } catch (e) {
            console.error('Preference field scan error:', e);
        }
    }

    isSalaryField(label, input) {
        const salaryKeywords = [
            'salary',
            'compensation',
            'expected salary',
            'desired salary',
            'annual salary',
            'salary expectations',
            'salary range',
            'ctc',
            'cost to company',
            'expected compensation',
        ];

        const lowerLabel = label.toLowerCase();
        const id = (input.id || '').toLowerCase();
        const name = (input.name || '').toLowerCase();
        const placeholder = (input.placeholder || '').toLowerCase();

        return salaryKeywords.some(
            (kw) =>
                lowerLabel.includes(kw) ||
                id.includes(kw) ||
                name.includes(kw) ||
                placeholder.includes(kw)
        );
    }

    isNoticePeriodField(label, input) {
        const noticePeriodKeywords = [
            'notice period',
            'notice',
            'availability',
            'start date',
            'available to start',
            'when can you start',
            'time to join',
        ];

        const lowerLabel = label.toLowerCase();
        const id = (input.id || '').toLowerCase();
        const name = (input.name || '').toLowerCase();
        const placeholder = (input.placeholder || '').toLowerCase();

        return noticePeriodKeywords.some(
            (kw) =>
                lowerLabel.includes(kw) ||
                id.includes(kw) ||
                name.includes(kw) ||
                placeholder.includes(kw)
        );
    }

    isVisaSponsorshipField(label, input) {
        const visaKeywords = [
            'sponsorship',
            'visa',
            'work authorization',
            'authorized to work',
            'visa status',
            'require sponsorship',
            'visa sponsorship',
            'work permit',
            'legal status',
        ];

        const lowerLabel = label.toLowerCase();
        const id = (input.id || '').toLowerCase();
        const name = (input.name || '').toLowerCase();
        const placeholder = (input.placeholder || '').toLowerCase();

        return visaKeywords.some(
            (kw) =>
                lowerLabel.includes(kw) ||
                id.includes(kw) ||
                name.includes(kw) ||
                placeholder.includes(kw)
        );
    }

    scanAndFillStandardFields() {
        if (!this.resumeParser) {
            return;
        }

        try {
            const inputs = document.querySelectorAll(
                'input[type="text"], input[type="email"], input[type="tel"], textarea'
            );

            inputs.forEach((input) => {
                if (!input.value) {
                    const label = this.getLabelTextForElement(input);
                    const lowerLabel = label.toLowerCase();

                    if (this.isFirstNameField(lowerLabel, input)) {
                        const firstName = this.extractFirstName(this.resumeParser.extractedData.name);
                        if (firstName) this.fillInputValue(input, firstName);
                    } else if (this.isLastNameField(lowerLabel, input)) {
                        const lastName = this.extractLastName(this.resumeParser.extractedData.name);
                        if (lastName) this.fillInputValue(input, lastName);
                    } else if (this.isFullNameField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.name) {
                            this.fillInputValue(input, this.resumeParser.extractedData.name);
                        }
                    } else if (this.isEmailField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.email) {
                            this.fillInputValue(input, this.resumeParser.extractedData.email);
                        }
                    } else if (this.isPhoneField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.phone) {
                            this.fillInputValue(input, this.resumeParser.extractedData.phone);
                        }
                    } else if (this.isLocationField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.location) {
                            this.fillInputValue(input, this.resumeParser.extractedData.location);
                        }
                    } else if (this.isJobTitleField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.currentJobTitle) {
                            this.fillInputValue(input, this.resumeParser.extractedData.currentJobTitle);
                        }
                    } else if (this.isExperienceField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.yearsOfExperience) {
                            this.fillInputValue(input, this.resumeParser.extractedData.yearsOfExperience);
                        }
                    } else if (this.isSkillsField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.skills) {
                            this.fillInputValue(input, this.resumeParser.extractedData.skills);
                        }
                    } else if (this.isEducationField(lowerLabel, input)) {
                        if (this.resumeParser.extractedData.education) {
                            this.fillInputValue(input, this.resumeParser.extractedData.education);
                        }
                    }
                }
            });
        } catch (e) {
            console.error('Standard field scan error:', e);
        }
    }

    extractFirstName(fullName) {
        return fullName ? fullName.split(' ')[0] : '';
    }

    extractLastName(fullName) {
        if (!fullName) return '';
        const parts = fullName.split(' ');
        return parts.length > 1 ? parts.slice(1).join(' ') : '';
    }

    isFirstNameField(label, input) {
        return /^(first\s+name|given\s+name|firstname)$/i.test(label) ||
               /first[\s-]?name/i.test(label) ||
               /fname/i.test((input.id || '') + (input.name || ''));
    }

    isLastNameField(label, input) {
        return /^(last\s+name|surname|lastname)$/i.test(label) ||
               /last[\s-]?name|surname/i.test(label) ||
               /lname|surname/i.test((input.id || '') + (input.name || ''));
    }

    isFullNameField(label, input) {
        return /^(full\s+name|name)$/i.test(label) ||
               /^name$/i.test(label) ||
               /fullname/i.test((input.id || '') + (input.name || ''));
    }

    isEmailField(label, input) {
        return /email|e-mail/i.test(label) ||
               /email/i.test((input.id || '') + (input.name || '') + (input.type || ''));
    }

    isPhoneField(label, input) {
        return /phone|mobile|contact\s+number|telephone/i.test(label) ||
               /phone|mobile/i.test((input.id || '') + (input.name || ''));
    }

    isLocationField(label, input) {
        return /location|city|state|address|based|country/i.test(label) ||
               /location|city/i.test((input.id || '') + (input.name || ''));
    }

    isJobTitleField(label, input) {
        return /job\s+title|current\s+position|position|role|designation/i.test(label) ||
               /job[\s-]?title|position/i.test((input.id || '') + (input.name || ''));
    }

    isExperienceField(label, input) {
        return /years?\s+of\s+experience|experience|work\s+experience|professional\s+experience/i.test(label) ||
               /experience|years/i.test((input.id || '') + (input.name || ''));
    }

    isSkillsField(label, input) {
        return /skills|expertise|competencies|proficiencies|technical\s+skills/i.test(label) ||
               /skills/i.test((input.id || '') + (input.name || ''));
    }

    isEducationField(label, input) {
        return /education|degree|qualification|graduated|university|college/i.test(label) ||
               /education|degree/i.test((input.id || '') + (input.name || ''));
    }

    executeAutofillSequence() {
        try {
            this.showToast('⚡ Starting autofill...', 'info', 2000);

            this.scanAndAttachResumePdf();
            this.scanAndFillPortfolioLinks();
            this.scanAndFillCoverLetters();
            this.scanAndFillPreferences();
            this.scanAndFillStandardFields();
            this.scanAndProcessForms();

            setTimeout(() => {
                this.showToast('✓ Autofill complete!', 'success', 3000);
            }, 1000);
        } catch (error) {
            console.error('Autofill sequence error:', error);
            this.showToast('❌ Autofill encountered an error', 'error', 3000);
        }
    }

    showToast(message, type = 'info', duration = 3000) {
        try {
            const toast = document.createElement('div');
            const colors = {
                info: { bg: '#3b82f6', text: 'white' },
                success: { bg: '#22c55e', text: 'white' },
                warning: { bg: '#f59e0b', text: 'white' },
                error: { bg: '#ef4444', text: 'white' },
            };
            const color = colors[type] || colors.info;

            toast.style.cssText = `
                position: fixed;
                bottom: 20px;
                right: 20px;
                padding: 12px 16px;
                background-color: ${color.bg};
                color: ${color.text};
                border-radius: 6px;
                font-size: 14px;
                font-weight: 500;
                z-index: 999999;
                animation: slideInUp 0.3s ease-out;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            `;

            toast.textContent = message;
            document.body.appendChild(toast);

            setTimeout(() => {
                toast.style.animation = 'slideOutDown 0.3s ease-out';
                setTimeout(() => toast.remove(), 300);
            }, duration);

            if (!document.querySelector('style[data-toast-animations]')) {
                const style = document.createElement('style');
                style.setAttribute('data-toast-animations', '');
                style.textContent = `
                    @keyframes slideInUp {
                        from { transform: translateY(20px); opacity: 0; }
                        to { transform: translateY(0); opacity: 1; }
                    }
                    @keyframes slideOutDown {
                        from { transform: translateY(0); opacity: 1; }
                        to { transform: translateY(20px); opacity: 0; }
                    }
                `;
                document.head.appendChild(style);
            }
        } catch (error) {
            console.error('Toast display error:', error);
        }
    }
}

let formEngine = null;

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        formEngine = new UniversalATSFormEngine();
    });
} else {
    formEngine = new UniversalATSFormEngine();
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'triggerAutofill') {
        if (!formEngine) {
            formEngine = new UniversalATSFormEngine();
        }

        try {
            formEngine.executeAutofillSequence();
            sendResponse({ success: true });
        } catch (error) {
            console.error('Autofill sequence error:', error);
            sendResponse({ success: false, error: error.message });
        }
    }
});

