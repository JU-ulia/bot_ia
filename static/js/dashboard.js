// Lógica de toggle da barra lateral mobile
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    
    if (sidebar.classList.contains('-translate-x-full')) {
        sidebar.classList.remove('-translate-x-full');
        backdrop.classList.remove('hidden');
    } else {
        sidebar.classList.add('-translate-x-full');
        backdrop.classList.add('hidden');
    }
}

// Instâncias globais dos gráficos
let categoryChartInstance = null;
let budgetChartInstance = null;
let balanceChartInstance = null;
let idUsuarioAtual = null;

// Executado assim que a página é carregada
document.addEventListener('DOMContentLoaded', async function () {
    initCategoryChart();
    initBudgetChart();
    initBalanceChart();
    
    // Define o mês atual por padrão no input (Ex: "2026-09")
    const hoje = new Date();
    const ano = hoje.getFullYear();
    const mes = String(hoje.getMonth() + 1).padStart(2, '0');
    const mesAtualFormatado = `${ano}-${mes}`;
    
    const inputMes = document.getElementById('filtro-mes');
    if (inputMes) {
        inputMes.value = mesAtualFormatado;
    }

    try {
        const userRes = await fetch('/api/usuario-atual');
        if (!userRes.ok) {
            window.location.href = '/login';
            return;
        }
        const user = await userRes.json();
        idUsuarioAtual = user.id || user.user_id || user.telegram_id || user.email || 'me';

        // Carrega dados do mês atual
        await carregarDadosDashboard(idUsuarioAtual, mesAtualFormatado);

        // Lembrete de e-mail digitado errado (reverifica a cada 60s, pois o aviso de devolução pode demorar)
        verificarEmailInvalido();
        setInterval(verificarEmailInvalido, 60000);

        // Atualiza os números sozinho (lançamentos feitos no Telegram aparecem sem recarregar a página)
        const recarregar = () => {
            if (!document.hidden && idUsuarioAtual) {
                const inputMes = document.getElementById('filtro-mes');
                carregarDadosDashboard(idUsuarioAtual, inputMes && inputMes.value ? inputMes.value : null);
            }
        };
        setInterval(recarregar, 30000);
        document.addEventListener('visibilitychange', recarregar);
    } catch (e) {
        // Não redireciona mais para /login aqui: só o 401 (acima) significa sessão inválida.
        // Antes, qualquer erro de JS mandava de volta ao login e parecia que "não entrava".
        console.error("Erro ao carregar o dashboard:", e);
    }
});

// Evento ao alterar o mês no input
async function aoMudarMes(mesSelecionado) {
    if (!mesSelecionado || !idUsuarioAtual) return;
    await carregarDadosDashboard(idUsuarioAtual, mesSelecionado);
}

// Busca e atualiza os dados do Dashboard
async function carregarDadosDashboard(usuarioId, mesFiltro = null) {
    try {
        let url = `/api/dados-dashboard/${usuarioId}`;
        if (mesFiltro) {
            url += `?mes=${mesFiltro}`;
        }

        const response = await fetch(url);
        if (!response.ok) throw new Error(`Erro na API (${response.status})`);

        const dados = await response.json();

        // 1. Atualiza Nome do Usuário
        if (dados.usuario) setUserName(dados.usuario);

        // Verifica se existem dados registrados no mês selecionado
        const possuiDados = dados.totais && (dados.totais.renda > 0 || dados.totais.despesa > 0 || dados.totais.poupanca > 0);

        // 2. Atualiza Poupança / Total Guardado
        const elPoupanca = document.getElementById('total-poupanca');
        if (elPoupanca && dados.totais) {
            const poupancaVal = dados.totais.poupanca || 0;
            elPoupanca.textContent = `R$ ${poupancaVal.toLocaleString('pt-BR', { minimumFractionDigits: 2 })}`;
        }

        // 3. Atualiza Gráfico de Orçamento Geral (Donut)
        if (budgetChartInstance && dados.totais) {
            if (!possuiDados) {
                budgetChartInstance.data.labels = ['Sem dados no mês'];
                budgetChartInstance.data.datasets[0].data = [1];
                budgetChartInstance.data.datasets[0].backgroundColor = ['#cbd5e1'];
            } else {
                budgetChartInstance.data.labels = ['Receita Mensal', 'Despesas Totais', 'Saldo Atual'];
                budgetChartInstance.data.datasets[0].data = [
                    dados.totais.renda || 0,
                    dados.totais.despesa || 0,
                    dados.totais.saldo > 0 ? dados.totais.saldo : 0
                ];
                budgetChartInstance.data.datasets[0].backgroundColor = ['#3b82f6', '#a855f7', '#f97316'];
            }
            budgetChartInstance.update();
        }

        // 4. Atualiza Gráfico de Gastos por Categoria (Barras)
        if (categoryChartInstance && dados.categorias) {
            if (!possuiDados || dados.categorias.labels.length === 0) {
                categoryChartInstance.data.labels = ['Sem dados no mês'];
                categoryChartInstance.data.datasets[0].data = [0];
            } else {
                categoryChartInstance.data.labels = dados.categorias.labels;
                categoryChartInstance.data.datasets[0].data = dados.categorias.valores;
            }
            categoryChartInstance.update();
        }

        // 5. ATUALIZAÇÃO DO GRÁFICO DE EVOLUÇÃO DO SALDO
        if (balanceChartInstance && dados.totais) {
            const valoresEvolucao = (dados.evolucao && dados.evolucao.valores && dados.evolucao.valores.length > 0) 
                ? dados.evolucao.valores 
                : [0, 0, 0, 0, 0, dados.totais.saldo];

            balanceChartInstance.data.datasets[0].data = valoresEvolucao;
            balanceChartInstance.update();
        }

        // 6. Atualiza Alerta de Risco com Cálculo Inteligente
        if (dados.totais) {
            const renda = dados.totais.renda || 0;
            const despesa = dados.totais.despesa || 0;
            
            if (!possuiDados || renda === 0) {
                updateAIAlerts(0, true, "Sem dados de renda suficientes para calcular o risco neste mês.");
            } else {
                const pctGasto = Math.round((despesa / renda) * 100);

                const hoje = new Date();
                const diaAtual = hoje.getDate();
                const ultimoDiaMes = new Date(hoje.getFullYear(), hoje.getMonth() + 1, 0).getDate();
                const pctDiasDecorridos = Math.round((diaAtual / ultimoDiaMes) * 100);

                let nivelRisco = "Baixo";
                let mensagemIA = "";

                if (pctGasto >= 85) {
                    nivelRisco = "Alto";
                    mensagemIA = `Atenção: Você comprometeu ${pctGasto}% da sua renda no dia ${diaAtual} do mês. Sua margem financeira está crítica!`;
                } else if (pctGasto > pctDiasDecorridos + 10) {
                    nivelRisco = "Médio";
                    mensagemIA = `Ritmo acelerado: Você já gastou ${pctGasto}% da renda, mas apenas ${pctDiasDecorridos}% do mês se passou.`;
                } else {
                    nivelRisco = "Baixo";
                    mensagemIA = `Excelente controle! Apenas ${pctGasto}% da renda gasta até o dia ${diaAtual} do mês.`;
                }

                updateAIAlerts(pctGasto, false, mensagemIA, nivelRisco);
            }
        }

        // 7. ATUALIZA STATUS DO CHATBOT DINAMICAMENTE
        if (dados.chatbot) {
            atualizarStatusChatbot(dados.chatbot.ultima_atividade, dados.chatbot.vinculado);
        }

    } catch (erro) {
        console.error("Erro ao carregar dados do banco:", erro);
        setUserName("Erro ao carregar");
    }
}

function updateAIAlerts(pctGasto, semDados = false, mensagemIA = "", nivelRisco = "Baixo") {
    const riskIconBoxEl = document.getElementById('risk-icon-box');
    const riskIconEl = document.getElementById('risk-icon');
    const riskStatusEl = document.getElementById('risk-status');
    const riskDescEl = document.getElementById('risk-description');
    const riskBarEl = document.getElementById('risk-bar');

    if (semDados) {
        if (riskIconBoxEl) riskIconBoxEl.className = "w-10 h-10 rounded-xl bg-slate-100 text-slate-500 flex items-center justify-center font-bold text-lg shadow-sm transition-colors";
        if (riskIconEl) riskIconEl.className = "fa-solid fa-circle-info text-slate-500";

        if (riskStatusEl) {
            riskStatusEl.textContent = 'Sem dados no mês';
            riskStatusEl.className = 'text-base font-bold text-slate-500';
        }
        if (riskBarEl) {
            riskBarEl.style.width = '0%';
            riskBarEl.className = 'h-full rounded-full bg-slate-300';
        }
        if (riskDescEl) {
            riskDescEl.innerHTML = `<span class="text-slate-500">${mensagemIA || 'Nenhum dado registrado para calcular risco.'}</span>`;
        }
        return;
    }

    let bgIcone = 'bg-emerald-100';
    let textIcone = 'text-emerald-600';
    let corTexto = 'text-emerald-600';
    let corBg = 'bg-emerald-500';
    let statusText = 'Situação Estável (Segura)';

    if (nivelRisco === "Alto" || pctGasto >= 85) {
        bgIcone = 'bg-red-100';
        textIcone = 'text-red-600';
        corTexto = 'text-red-600';
        corBg = 'bg-red-500';
        statusText = 'Zona de Alto Risco';
    } else if (nivelRisco === "Médio" || pctGasto > 60) {
        bgIcone = 'bg-amber-100';
        textIcone = 'text-amber-600';
        corTexto = 'text-amber-600';
        corBg = 'bg-amber-500';
        statusText = 'Atenção (Risco Moderado)';
    }

    if (riskIconBoxEl) {
        riskIconBoxEl.className = `w-10 h-10 rounded-xl ${bgIcone} ${textIcone} flex items-center justify-center font-bold text-lg shadow-sm transition-colors`;
    }
    if (riskIconEl) {
        riskIconEl.className = `fa-solid fa-triangle-exclamation ${textIcone}`;
    }

    if (riskStatusEl) {
        riskStatusEl.textContent = statusText;
        riskStatusEl.className = `text-base font-bold ${corTexto}`;
    }

    if (riskBarEl) {
        riskBarEl.style.width = `${Math.min(pctGasto, 100)}%`;
        riskBarEl.className = `h-full rounded-full transition-all duration-500 ${corBg}`;
    }

    if (riskDescEl) {
        riskDescEl.innerHTML = `
            <div class="space-y-3 mt-1">
                <p class="text-sm text-slate-600">
                    Despesas Atuais: <span class="font-bold ${corTexto}">${pctGasto}%</span> da Renda Estimada
                </p>

                <div class="p-3 bg-amber-50 border border-amber-200/80 rounded-xl flex items-start gap-2.5 text-left">
                    <i class="fa-solid fa-wand-magic-sparkles text-amber-500 text-sm mt-0.5 shrink-0"></i>
                    <div class="text-xs text-amber-900 leading-relaxed">
                        <strong class="font-semibold block mb-0.5">Estimado com IA:</strong>
                        ${mensagemIA}
                    </div>
                </div>
            </div>
        `;
    }
}

function initCategoryChart() {
    const canvas = document.getElementById('categoryChart');
    if (!canvas) return;
    categoryChartInstance = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
            labels: ['Sem dados'],
            datasets: [{ label: 'Despesas (R$)', data: [0], backgroundColor: '#3b82f6', borderRadius: 4 }]
        },
        options: { responsive: true, maintainAspectRatio: false }
    });
}

function initBudgetChart() {
    const canvas = document.getElementById('budgetChart');
    if (!canvas) return;
    budgetChartInstance = new Chart(canvas.getContext('2d'), {
        type: 'doughnut',
        data: {
            labels: ['Receita Mensal', 'Despesas Totais', 'Saldo Atual'],
            datasets: [{ data: [0, 0, 0], backgroundColor: ['#3b82f6', '#a855f7', '#f97316'] }]
        },
        options: { responsive: true, maintainAspectRatio: false }
    });
}

function obterUltimosMeses(quantidade = 6) {
    const nomesMeses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"];
    const hoje = new Date();
    const labels = [];

    for (let i = quantidade - 1; i >= 0; i--) {
        const d = new Date(hoje.getFullYear(), hoje.getMonth() - i, 1);
        labels.push(nomesMeses[d.getMonth()]);
    }
    return labels;
}

function initBalanceChart() {
    const canvas = document.getElementById('balanceChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    const gradient = ctx.createLinearGradient(0, 0, 0, 300);
    gradient.addColorStop(0, 'rgba(59, 130, 246, 0.4)');
    gradient.addColorStop(1, 'rgba(59, 130, 246, 0.0)');

    const mesesDinamicos = obterUltimosMeses(6);

    balanceChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: mesesDinamicos,
            datasets: [{
                label: 'Saldo Acumulado (R$)',
                data: [0, 0, 0, 0, 0, 0],
                borderColor: '#2563eb',
                borderWidth: 3,
                backgroundColor: gradient,
                fill: true,
                tension: 0.35,
                pointRadius: 4,
                pointBackgroundColor: '#2563eb'
            }]
        },
        options: { 
            responsive: true, 
            maintainAspectRatio: false,
            scales: {
                x: { grid: { display: false } },
                y: { grid: { color: '#f1f5f9' }, ticks: { callback: (val) => `R$ ${val}` } }
            }
        }
    });
}

function setUserName(name) {
    const el = document.getElementById('user-name');
    if (el) el.textContent = name;
}

function atualizarStatusChatbot(ultimaAtividade, vinculado = false) {
    const badgeEl = document.getElementById('bot-status-badge');
    const updateTextEl = document.getElementById('bot-last-update');
    const boxEl = document.getElementById('bot-connection-box');
    const connTextEl = document.getElementById('bot-connection-text');

    if (ultimaAtividade || vinculado) {
        if (badgeEl) {
            badgeEl.className = "p-1.5 bg-emerald-100 text-emerald-700 rounded-lg text-xs font-semibold flex items-center gap-1";
            badgeEl.innerHTML = `<i class="fa-solid fa-robot"></i> Ativo`;
        }
        if (updateTextEl) {
            updateTextEl.textContent = ultimaAtividade || 'Nenhum lançamento neste mês ainda';
        }
        if (boxEl && connTextEl) {
            boxEl.className = "p-3 bg-emerald-50 border border-emerald-200/60 rounded-xl transition-all";
            connTextEl.className = "text-emerald-800 text-sm font-medium flex items-center gap-2";
            connTextEl.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-500"></i> Conectado ao Telegram`;
        }
    } else {
        if (badgeEl) {
            badgeEl.className = "p-1.5 bg-amber-100 text-amber-700 rounded-lg text-xs font-semibold flex items-center gap-1";
            badgeEl.innerHTML = `<i class="fa-solid fa-robot"></i> Aguardando Vínculo`;
        }
        if (updateTextEl) {
            updateTextEl.textContent = "Aguardando mensagens ou vínculo via /login";
        }
        if (boxEl && connTextEl) {
            boxEl.className = "p-3 bg-amber-50 border border-amber-200/60 rounded-xl transition-all";
            connTextEl.className = "text-amber-800 text-sm font-medium flex items-center gap-2";
            connTextEl.innerHTML = `<i class="fa-solid fa-circle-exclamation text-amber-500"></i> Envie /login no Telegram para sincronizar`;
        }
    }
}

// ==========================================
// LEMBRETE: E-MAIL DIGITADO ERRADO
// ==========================================
function escaparHtml(texto) {
    const d = document.createElement('div');
    d.textContent = texto == null ? '' : String(texto);
    return d.innerHTML;
}

function obterContainerAlertaEmail() {
    let container = document.getElementById('email-alert-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'email-alert-container';
        (document.querySelector('main') || document.body).prepend(container);
    }
    return container;
}

async function verificarEmailInvalido() {
    try {
        const res = await fetch('/api/email-status');
        if (res.status === 401) {
            // Sessão criada antes do user_id existir: sai para entrar de novo com a sessão atualizada
            window.location.href = '/logout';
            return;
        }
        if (!res.ok) return;
        const info = await res.json();

        if (info.email_invalido) {
            mostrarAlertaEmail(info.email);
        } else {
            const container = document.getElementById('email-alert-container');
            if (container) container.innerHTML = '';
        }
    } catch (e) {
        console.error('Erro ao verificar e-mail:', e);
    }
}

function mostrarAlertaEmail(emailErrado) {
    const container = obterContainerAlertaEmail();
    // Não redesenha enquanto o usuário está digitando o novo e-mail
    if (container.dataset.editando === '1') return;

    container.innerHTML = `
        <div class="mb-4 p-4 bg-red-50 border border-red-200 rounded-xl">
            <div class="flex items-start gap-3">
                <i class="fa-solid fa-triangle-exclamation text-red-500 mt-0.5"></i>
                <div class="flex-1 text-sm text-red-900">
                    <p>
                        Não conseguimos entregar e-mails para <strong>${escaparHtml(emailErrado)}</strong>.
                        Parece que ele foi digitado errado.
                        <button type="button" onclick="abrirEdicaoEmail()" class="font-semibold underline hover:text-red-700">Clique aqui para editar</button>
                    </p>

                    <div id="email-edit-area" class="hidden mt-3 flex flex-col sm:flex-row gap-2">
                        <input type="email" id="novo-email-input" placeholder="seu@email.com"
                               class="flex-1 px-3 py-2 rounded-lg border border-red-200 bg-white text-slate-800 focus:outline-none focus:border-red-400">
                        <button type="button" id="btn-salvar-email" onclick="salvarNovoEmail()"
                                class="px-4 py-2 bg-red-600 hover:bg-red-500 text-white font-semibold rounded-lg transition">
                            Salvar
                        </button>
                    </div>
                    <p id="email-edit-msg" class="hidden mt-2 text-xs font-medium"></p>
                </div>
            </div>
        </div>
    `;
}

function abrirEdicaoEmail() {
    const container = obterContainerAlertaEmail();
    container.dataset.editando = '1';
    document.getElementById('email-edit-area').classList.remove('hidden');
    document.getElementById('novo-email-input').focus();
}

async function salvarNovoEmail() {
    const input = document.getElementById('novo-email-input');
    const btn = document.getElementById('btn-salvar-email');
    const msgEl = document.getElementById('email-edit-msg');
    const novoEmail = input.value.trim();

    msgEl.classList.remove('hidden');
    if (!novoEmail || !novoEmail.includes('@')) {
        msgEl.className = 'mt-2 text-xs font-medium text-red-700';
        msgEl.textContent = 'Digite um e-mail válido.';
        return;
    }

    btn.disabled = true;
    btn.textContent = 'Salvando...';

    try {
        const res = await fetch('/api/atualizar-email', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: novoEmail })
        });
        const data = await res.json();

        if (res.ok) {
            const container = obterContainerAlertaEmail();
            container.dataset.editando = '0';
            container.innerHTML = `
                <div class="mb-4 p-4 bg-emerald-50 border border-emerald-200 rounded-xl text-sm text-emerald-800 flex items-center gap-2">
                    <i class="fa-solid fa-circle-check text-emerald-500"></i>
                    E-mail atualizado para <strong>${escaparHtml(data.email)}</strong>. Enviamos uma mensagem de confirmação.
                </div>`;
            setTimeout(() => { container.innerHTML = ''; }, 6000);
        } else {
            msgEl.className = 'mt-2 text-xs font-medium text-red-700';
            msgEl.textContent = data.erro || 'Não foi possível atualizar o e-mail.';
            btn.disabled = false;
            btn.textContent = 'Salvar';
        }
    } catch (e) {
        msgEl.className = 'mt-2 text-xs font-medium text-red-700';
        msgEl.textContent = 'Erro de conexão com o servidor.';
        btn.disabled = false;
        btn.textContent = 'Salvar';
    }
}