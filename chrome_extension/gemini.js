class GeminiClient {
    constructor(apiKey) {
        this.apiKey = apiKey;
        this.baseUrl = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent';
        this.requestCount = 0;
        this.rateLimitDelay = 100;
    }

    async generateAnswer(question, resume) {
        if (!this.apiKey) {
            throw new Error('API key not configured');
        }

        const payload = {
            contents: [
                {
                    parts: [
                        {
                            text: this.buildPrompt(question, resume),
                        },
                    ],
                },
            ],
            generationConfig: {
                temperature: 0.7,
                maxOutputTokens: 500,
            },
        };

        try {
            await this.applyRateLimit();
            const response = await this.makeRequest(payload);

            if (!response.ok) {
                const error = await response.json();

                if (response.status === 429) {
                    throw new Error('429: Rate limit exceeded. Please wait before trying again.');
                }

                throw new Error(
                    error.error?.message || `HTTP ${response.status}: API request failed`
                );
            }

            const data = await response.json();
            const text =
                data.candidates?.[0]?.content?.parts?.[0]?.text || '';

            if (!text) {
                throw new Error('Empty response from API');
            }

            return this.sanitizeAnswer(text);
        } catch (error) {
            console.error('Gemini API error:', error);
            throw error;
        }
    }

    async makeRequest(payload) {
        try {
            return await fetch(`${this.baseUrl}?key=${this.apiKey}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });
        } catch (networkError) {
            console.error('Network error:', networkError);
            throw new Error(`Network error: ${networkError.message}`);
        }
    }

    async applyRateLimit() {
        this.requestCount++;
        if (this.requestCount > 1) {
            await new Promise((resolve) =>
                setTimeout(resolve, this.rateLimitDelay)
            );
        }
    }

    buildPrompt(question, resume) {
        return `You are answering a job application screening question based on the provided resume. Your response should be natural, grounded, and human-like.

RESPONSE GUIDELINES:
- Write in first-person perspective: "I built...", "I've worked with...", "My background includes..."
- Be concise and direct: 2-4 sentences maximum. Answer exactly what was asked.
- Sound like a real person, not an AI. Use natural language, not corporate jargon.
- Ground your answer in resume facts. Don't invent or exaggerate skills.
- Avoid overused phrases: spearheaded, testament, delve, passionate, leverage, cutting-edge, paradigm shift, synergy.
- Avoid repeating the question. Just provide the answer.
- Be honest. If the question doesn't apply to your background, say so briefly.

Question: ${question}

Resume:
${resume}

Answer:`;
    }

    sanitizeAnswer(text) {
        return text
            .trim()
            .replace(/^["']|["']$/g, '')
            .replace(/\*\*/g, '')
            .replace(/^#\s+/gm, '')
            .slice(0, 500);
    }

    async testConnection() {
        try {
            const response = await fetch(
                `${this.baseUrl}?key=${this.apiKey}`,
                {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        contents: [
                            {
                                parts: [
                                    {
                                        text: 'Say OK in one word.',
                                    },
                                ],
                            },
                        ],
                    }),
                }
            );

            return response.ok;
        } catch (error) {
            console.error('Connection test failed:', error);
            return false;
        }
    }
}
