#!/usr/bin/env python3
"""
Nodo do S3 De Pobre
Cada nodo atua como servidor e cliente simultaneamente
"""

import os
import sys
import time
import socket
import threading
import json
import subprocess
import io
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file
from threading import Lock
import requests

# env vars
load_dotenv()

# Classe do Nodo
class Nodo:
    def __init__(self, id_nodo):
        self.id_nodo = id_nodo
        self.portas = list(map(int, os.getenv('PORTAS', '').split(',')))
        self.porta = self.portas[id_nodo - 1]
        self.portas_http = list(map(int, os.getenv('PORTAS_HTTP', '').split(',')))
        self.porta_http = self.portas_http[id_nodo - 1]
        self.intervalo_heartbeat = int(os.getenv('INTERVALO_HEARTBEAT', '5'))
        self.timeout_heartbeat = int(os.getenv('TIMEOUT_HEARTBEAT', '15'))
        
        # dirs do nodo
        self.dir_arquivos = Path(f'files_nodo_{id_nodo}')
        self.dir_log = Path('log')
        self.arquivo_log = self.dir_log / f'nodo_{id_nodo}.log'
        self.arquivo_bd = Path('files_db.json')  # Banco de dados compartilhado em formato JSON
        
        # cria dirs do nodo se não houver, exist_ok do pathlib pra não dar exception se existe
        self.dir_arquivos.mkdir(exist_ok=True)
        self.dir_log.mkdir(exist_ok=True)
        
        # lock pra acesso thread-safe ao banco de dados
        self.lock_bd = Lock()
        
        # inicializa banco de dados
        self._inicializar_bd()
        
        # manter estados dos nodos durante heartbeat
        self.status_nodos = {}  # {porta: ultimo_heartbeat}
        self.tentativas_recuperacao = {}  # {porta: ultima_tentativa} para evitar tentativas duplicadas
        
        # estado do nodo
        self.rodando = True
        self.socket_servidor = None
        self.tempo_inicializacao = time.time()  # Timestamp de quando o nodo foi criado
        
        # Flask app
        self.app = Flask(f'nodo_{id_nodo}')
        self._configurar_rotas()
        
    # Registra mensagens no arquivo de log com timestamp
    def registrar_log(self, mensagem):
        """Registra mensagem no log com timestamp"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        mensagem_log = f'[{timestamp}] {mensagem}'
        print(mensagem_log)
        
        with open(self.arquivo_log, 'a') as f:
            f.write(mensagem_log + '\n')
    
    # Inicializa o banco de dados JSON compartilhado se não existir
    def _inicializar_bd(self):
        """Inicializa o banco de dados JSON compartilhado"""
        with self.lock_bd:
            if not self.arquivo_bd.exists():
                dados_iniciais = {
                    'ultimo_id': 0,
                    'arquivos': {},  # {id_arquivo: {nome, tamanho, fragmentos: [{id_nodo, id_fragmento, tamanho}]}}
                    'armazenamento_nodo': {i: 0 for i in range(1, 9)}  # Bytes armazenados por nodo
                }
                with open(self.arquivo_bd, 'w') as f:
                    json.dump(dados_iniciais, f, indent=2)
    
    # Lê o banco de dados JSON com lock para thread-safety
    def _ler_bd(self):
        """Lê o banco de dados"""
        with self.lock_bd:
            with open(self.arquivo_bd, 'r') as f:
                return json.load(f)
    
    # Escreve no banco de dados JSON com lock para thread-safety
    def _escrever_bd(self, dados):
        """Escreve no banco de dados"""
        with self.lock_bd:
            with open(self.arquivo_bd, 'w') as f:
                json.dump(dados, f, indent=2)
    
    # Retorna os nodos com menor carga de armazenamento para balanceamento
    def _obter_nodos_menos_carregados(self, quantidade):
        """Retorna os 'quantidade' nodos com menor armazenamento"""
        bd = self._ler_bd()
        armazenamento = bd['armazenamento_nodo']
        nodos_ordenados = sorted(armazenamento.items(), key=lambda x: x[1])
        return [int(id_nodo) for id_nodo, _ in nodos_ordenados[:quantidade]]
    
    # Fragmenta o arquivo em partes baseado no tamanho e define número de réplicas
    def _fragmentar_arquivo(self, dados_arquivo, nome_arquivo):
        """Fragmenta arquivo e retorna fragmentos com estratégia de distribuição"""
        tamanho_arquivo = len(dados_arquivo)
        
        if tamanho_arquivo <= 100:
            # Arquivo pequeno: 1 fragmento + 1 réplica
            num_fragmentos = 1
            replicas_por_fragmento = 2
        elif tamanho_arquivo <= 1024:
            # Arquivo médio: 2 fragmentos + 2 réplicas cada
            num_fragmentos = 2
            replicas_por_fragmento = 2
        else:
            # Arquivo grande: 4 fragmentos + 2 réplicas cada
            num_fragmentos = 4
            replicas_por_fragmento = 4
        
        # Divisão inteira do arquivo
        tamanho_fragmento = len(dados_arquivo) // num_fragmentos
        fragmentos = []
        
        for i in range(num_fragmentos):
            inicio = i * tamanho_fragmento
            if i == num_fragmentos - 1:
                # Último fragmento pega o resto
                fim = len(dados_arquivo)
            else:
                fim = inicio + tamanho_fragmento
            
            dados_fragmento = dados_arquivo[inicio:fim]
            fragmentos.append({
                'id_fragmento': i,
                'dados': dados_fragmento,
                'tamanho': len(dados_fragmento)
            })
        
        return fragmentos, replicas_por_fragmento
    
    # Distribui os fragmentos entre os nodos e atualiza o banco de dados
    def _distribuir_fragmentos(self, fragmentos, replicas_por_fragmento, id_arquivo, nome_arquivo):
        """Distribui fragmentos entre os nodos com menor carga"""
        bd = self._ler_bd()
        localizacoes_fragmentos = []
        
        for fragmento in fragmentos:
            # Escolhe nodos com menor carga para este fragmento
            nodos_alvo = self._obter_nodos_menos_carregados(replicas_por_fragmento)
            
            for id_nodo in nodos_alvo:
                # Salva o fragmento
                nome_fragmento = f'file_{id_arquivo}_frag_{fragmento["id_fragmento"]}'
                
                if id_nodo == self.id_nodo:
                    # Salva localmente
                    caminho_fragmento = self.dir_arquivos / nome_fragmento
                    with open(caminho_fragmento, 'wb') as f:
                        f.write(fragmento['dados'])
                    self.registrar_log(f'Fragmento {fragmento["id_fragmento"]} do arquivo {id_arquivo} salvo localmente')
                else:
                    # Envia para outro nodo
                    self._enviar_fragmento_para_nodo(id_nodo, id_arquivo, fragmento['id_fragmento'], fragmento['dados'])
                
                # Atualiza o armazenamento do nodo
                bd['armazenamento_nodo'][str(id_nodo)] += fragmento['tamanho']
                
                localizacoes_fragmentos.append({
                    'id_nodo': id_nodo,
                    'id_fragmento': fragmento['id_fragmento'],
                    'tamanho': fragmento['tamanho']
                })
        
        # Atualiza banco de dados
        bd['arquivos'][str(id_arquivo)] = {
            'nome': nome_arquivo,
            'tamanho': sum(f['tamanho'] for f in fragmentos),
            'fragmentos': localizacoes_fragmentos
        }
        self._escrever_bd(bd)
        
        return localizacoes_fragmentos
    
    # Envia um fragmento para outro nodo via requisição HTTP POST
    def _enviar_fragmento_para_nodo(self, id_nodo_alvo, id_arquivo, id_fragmento, dados):
        """Envia um fragmento para outro nodo via HTTP"""
        try:
            porta_alvo = self.portas_http[id_nodo_alvo - 1]
            url = f'http://localhost:{porta_alvo}/store_fragment'
            
            arquivos = {'fragment': (f'file_{id_arquivo}_frag_{id_fragmento}', io.BytesIO(dados))}
            resposta = requests.post(url, files=arquivos, timeout=5)
            
            if resposta.status_code == 200:
                self.registrar_log(f'Fragmento {id_fragmento} do arquivo {id_arquivo} enviado para nodo {id_nodo_alvo}')
            else:
                self.registrar_log(f'ERRO ao enviar fragmento para nodo {id_nodo_alvo}: {resposta.status_code}')
        except Exception as e:
            self.registrar_log(f'ERRO ao enviar fragmento para nodo {id_nodo_alvo}: {e}')
    
    # Configura todas as rotas HTTP do servidor Flask
    def _configurar_rotas(self):
        """Configura rotas HTTP do Flask"""
        
        @self.app.route('/upload', methods=['POST'])
        def upload():
            try:
                if 'file' not in request.files:
                    return jsonify({'error': 'Nenhum arquivo enviado'}), 400
                
                arquivo = request.files['file']
                if arquivo.filename == '':
                    return jsonify({'error': 'Nome de arquivo vazio'}), 400
                
                # Lê o arquivo
                dados_arquivo = arquivo.read()
                nome_arquivo = arquivo.filename
                
                # Gera novo ID
                bd = self._ler_bd()
                id_arquivo = bd['ultimo_id'] + 1
                bd['ultimo_id'] = id_arquivo
                self._escrever_bd(bd)
                
                # Fragmenta e distribui
                fragmentos, replicas = self._fragmentar_arquivo(dados_arquivo, nome_arquivo)
                localizacoes = self._distribuir_fragmentos(fragmentos, replicas, id_arquivo, nome_arquivo)
                
                self.registrar_log(f'Arquivo {nome_arquivo} (ID: {id_arquivo}) recebido e distribuído')
                
                return jsonify({
                    'id': id_arquivo,
                    'filename': nome_arquivo,
                    'size': len(dados_arquivo),
                    'fragments': len(fragmentos),
                    'locations': localizacoes
                }), 200
                
            except Exception as e:
                self.registrar_log(f'ERRO no upload: {e}')
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/download/<int:file_id>', methods=['GET'])
        def download(file_id):
            try:
                bd = self._ler_bd()
                
                if str(file_id) not in bd['arquivos']:
                    return jsonify({'error': 'Arquivo não encontrado'}), 404
                
                info_arquivo = bd['arquivos'][str(file_id)]
                
                # Agrupa fragmentos por id_fragmento
                fragmentos_por_id = {}
                for frag in info_arquivo['fragmentos']:
                    id_frag = frag['id_fragmento']
                    if id_frag not in fragmentos_por_id:
                        fragmentos_por_id[id_frag] = []
                    fragmentos_por_id[id_frag].append(frag['id_nodo'])
                
                # Reconstrói o arquivo
                dados_arquivo = b''
                for id_frag in sorted(fragmentos_por_id.keys()):
                    # Tenta buscar de um dos nodos que tem este fragmento
                    dados_fragmento = None
                    for id_nodo in fragmentos_por_id[id_frag]:
                        dados_fragmento = self._obter_fragmento(file_id, id_frag, id_nodo)
                        if dados_fragmento:
                            break
                    
                    if dados_fragmento is None:
                        return jsonify({'error': f'Fragmento {id_frag} não encontrado'}), 500
                    
                    dados_arquivo += dados_fragmento
                
                self.registrar_log(f'Arquivo {file_id} ({info_arquivo["nome"]}) baixado')
                
                return send_file(
                    io.BytesIO(dados_arquivo),
                    download_name=info_arquivo['nome'],
                    as_attachment=True
                )
                
            except Exception as e:
                self.registrar_log(f'ERRO no download: {e}')
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/store_fragment', methods=['POST'])
        def store_fragment():
            """Recebe e armazena um fragmento enviado por outro nodo"""
            try:
                if 'fragment' not in request.files:
                    return jsonify({'error': 'Nenhum fragmento enviado'}), 400
                
                arquivo_fragmento = request.files['fragment']
                nome_fragmento = arquivo_fragmento.filename
                
                # Salva o fragmento
                caminho_fragmento = self.dir_arquivos / nome_fragmento
                arquivo_fragmento.save(caminho_fragmento)
                
                return jsonify({'status': 'ok'}), 200
                
            except Exception as e:
                self.registrar_log(f'ERRO ao armazenar fragmento: {e}')
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/list', methods=['GET'])
        def list_files():
            """Lista todos os arquivos disponíveis"""
            try:
                bd = self._ler_bd()
                lista_arquivos = []
                
                for id_arquivo, info_arquivo in bd['arquivos'].items():
                    lista_arquivos.append({
                        'id': int(id_arquivo),
                        'name': info_arquivo['nome'],
                        'size': info_arquivo['tamanho']
                    })
                
                return jsonify({'files': lista_arquivos}), 200
                
            except Exception as e:
                self.registrar_log(f'ERRO ao listar arquivos: {e}')
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/get_fragment/<fragment_filename>', methods=['GET'])
        def get_fragment(fragment_filename):
            """Retorna um fragmento armazenado localmente"""
            try:
                caminho_fragmento = self.dir_arquivos / fragment_filename
                
                if not caminho_fragmento.exists():
                    return jsonify({'error': 'Fragmento não encontrado'}), 404
                
                return send_file(caminho_fragmento, as_attachment=True)
                
            except Exception as e:
                self.registrar_log(f'ERRO ao buscar fragmento: {e}')
                return jsonify({'error': str(e)}), 500
    
    # Busca um fragmento específico localmente ou de outro nodo via HTTP
    def _obter_fragmento(self, id_arquivo, id_fragmento, id_nodo):
        """Busca um fragmento de um nodo específico"""
        nome_fragmento = f'file_{id_arquivo}_frag_{id_fragmento}'
        
        if id_nodo == self.id_nodo:
            # Fragmento local
            caminho_fragmento = self.dir_arquivos / nome_fragmento
            if caminho_fragmento.exists():
                with open(caminho_fragmento, 'rb') as f:
                    return f.read()
        else:
            # Fragmento em outro nodo
            try:
                porta_alvo = self.portas_http[id_nodo - 1]
                url = f'http://localhost:{porta_alvo}/get_fragment/{nome_fragmento}'
                resposta = requests.get(url, timeout=5)
                
                if resposta.status_code == 200:
                    return resposta.content
            except Exception as e:
                self.registrar_log(f'ERRO ao buscar fragmento de nodo {id_nodo}: {e}')
        
        return None
    
    # Inicia o servidor TCP para comunicação entre nodos (heartbeat)
    def iniciar_servidor(self):
        """Inicia o servidor do nodo"""
        self.registrar_log('iniciando')
        
        self.socket_servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket_servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.socket_servidor.bind(('localhost', self.porta))
            self.socket_servidor.listen(5)
            self.registrar_log(f'iniciado na porta {self.porta}')
            
            # Thread para aceitar conexões
            thread_aceitar = threading.Thread(target=self.aceitar_conexoes)
            thread_aceitar.daemon = True
            thread_aceitar.start()
            
        except Exception as e:
            self.registrar_log(f'ERRO ao iniciar servidor: {e}')
            sys.exit(1)
    
    # Aceita conexões TCP de outros nodos em loop contínuo
    def aceitar_conexoes(self):
        """Aceita conexões de outros nodos"""
        while self.rodando:
            try:
                self.socket_servidor.settimeout(1.0)
                socket_cliente, endereco = self.socket_servidor.accept()
                
                # Processa a conexão em uma nova thread
                thread = threading.Thread(target=self.processar_conexao, args=(socket_cliente,))
                thread.daemon = True
                thread.start()
                
            except socket.timeout:
                continue
            except Exception as e:
                if self.rodando:
                    self.registrar_log(f'ERRO ao aceitar conexão: {e}')
    
    # Processa uma conexão TCP recebida (principalmente heartbeats)
    def processar_conexao(self, socket_cliente):
        """Processa uma conexão recebida"""
        try:
            dados = socket_cliente.recv(1024).decode('utf-8')
            
            if not dados:
                return
            
            mensagem = json.loads(dados)
            
            if mensagem.get('type') == 'heartbeat':
                # Responde ao heartbeat
                resposta = {'type': 'heartbeat_ack', 'node_id': self.id_nodo, 'port': self.porta}
                socket_cliente.send(json.dumps(resposta).encode('utf-8'))
                
        except Exception as e:
            self.registrar_log(f'ERRO ao processar conexão: {e}')
        finally:
            socket_cliente.close()
    
    # Envia heartbeat para um nodo específico e aguarda resposta
    def enviar_heartbeat(self, porta):
        """Envia heartbeat para um nodo específico"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(('localhost', porta))
            
            mensagem = {'type': 'heartbeat', 'node_id': self.id_nodo, 'port': self.porta}
            sock.send(json.dumps(mensagem).encode('utf-8'))
            
            # Aguarda resposta
            resposta = sock.recv(1024).decode('utf-8')
            if resposta:
                self.status_nodos[porta] = time.time()
                return True
            
        except Exception:
            return False
        finally:
            try:
                sock.close()
            except:
                pass
        
        return False
    
    # Monitora continuamente o status de todos os nodos via heartbeat
    def monitorar_heartbeat(self):
        """Monitora heartbeat de todos os nodos"""
        while self.rodando:
            tempo_atual = time.time()
            nodos_vivos = []
            nodos_mortos = []
            
            # Verifica todos os nodos (exceto o próprio)
            for porta in self.portas:
                if porta == self.porta:
                    continue
                
                # Envia heartbeat
                if self.enviar_heartbeat(porta):
                    nodos_vivos.append(porta)
                else:
                    # Verifica se está morto há tempo suficiente
                    ultimo_visto = self.status_nodos.get(porta, 0)
                    if tempo_atual - ultimo_visto > self.timeout_heartbeat:
                        if porta not in nodos_mortos:
                            nodos_mortos.append(porta)
            
            # Log de status
            self.registrar_log(f'Nodos vivos: {nodos_vivos}')
            
            if nodos_mortos:
                self.registrar_log(f'Nodos mortos detectados: {nodos_mortos}')
                # Tenta recuperar nodos mortos
                for porta in nodos_mortos:
                    self.tentar_recuperar_nodo(porta)
            
            # Aguarda até o próximo heartbeat
            time.sleep(self.intervalo_heartbeat)
    
    # Tenta reiniciar um nodo que foi detectado como morto
    def tentar_recuperar_nodo(self, porta):
        """Tenta recuperar um nodo que caiu"""
        tempo_atual = time.time()
        
        # Não tenta recuperar nodos durante o período de inicialização (primeiros 15 segundos)
        if tempo_atual - self.tempo_inicializacao < 15:
            return  # Ainda no período de inicialização global
        
        # Verifica se já tentamos recuperar esse nodo recentemente (cooldown de 30 segundos)
        ultima_tentativa = self.tentativas_recuperacao.get(porta, 0)
        if tempo_atual - ultima_tentativa < self.timeout_heartbeat*2:
            return  # Evita múltiplas tentativas simultâneas
        
        # Encontra o ID do nodo pela porta
        try:
            id_nodo = self.portas.index(porta) + 1
            self.registrar_log(f'Tentando recuperar nodo {id_nodo} na porta {porta}')
            
            # Registra a tentativa de recuperação
            self.tentativas_recuperacao[porta] = tempo_atual
            
            # Inicia um novo processo do nodo
            # Usa o mesmo interpretador Python que está rodando este script
            executavel_python = sys.executable
            caminho_script = os.path.abspath(__file__)
            
            # Inicia o processo em background
            subprocess.Popen(
                [executavel_python, caminho_script, str(id_nodo)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True  # Desacopla do processo pai
            )
            
            self.registrar_log(f'Processo de recuperação iniciado para nodo {id_nodo}')
            
        except Exception as e:
            self.registrar_log(f'ERRO ao tentar recuperar nodo na porta {porta}: {e}')
    
    # Função principal que inicia todos os componentes do nodo
    def executar(self):
        """Executa o nodo"""
        # Inicia o servidor TCP
        self.iniciar_servidor()
        
        # Inicia servidor HTTP em thread separada
        thread_http = threading.Thread(target=self._iniciar_servidor_http)
        thread_http.daemon = True
        thread_http.start()
        
        # Aguarda tempo suficiente para garantir que TODOS os nodos (inclusive HTTP) estejam prontos
        # Isso evita que nodos tentem recuperar outros que ainda estão iniciando
        tempo_espera = 10
        self.registrar_log(f'Aguardando {tempo_espera}s para sincronização inicial dos nodos...')
        time.sleep(tempo_espera)
        
        # Inicia monitor de heartbeat
        thread_heartbeat = threading.Thread(target=self.monitorar_heartbeat)
        thread_heartbeat.daemon = True
        thread_heartbeat.start()
        
        self.registrar_log(f'Sistema iniciado - TCP:{self.porta} HTTP:{self.porta_http}')
        
        # Loop principal
        try:
            while self.rodando:
                time.sleep(1)
        except KeyboardInterrupt:
            self.registrar_log('Encerrando nodo...')
            self.rodando = False
            if self.socket_servidor:
                self.socket_servidor.close()
    
    # Inicia o servidor HTTP Flask para receber requisições de upload/download
    def _iniciar_servidor_http(self):
        """Inicia o servidor HTTP Flask"""
        try:
            self.registrar_log(f'Servidor HTTP iniciando na porta {self.porta_http}...')
            self.app.run(
                host='0.0.0.0',
                port=self.porta_http,
                debug=False,
                use_reloader=False,
                threaded=True
            )
        except Exception as e:
            self.registrar_log(f'ERRO ao iniciar servidor HTTP: {e}')


# Função principal de entrada do programa
def main():
    if len(sys.argv) != 2:
        print('Uso: python node.py <id_nodo>')
        print('id_nodo deve ser entre 1 e 8')
        sys.exit(1)
    
    try:
        id_nodo = int(sys.argv[1])
        if id_nodo < 1 or id_nodo > 8:
            raise ValueError('id_nodo deve ser entre 1 e 8')
        
        nodo = Nodo(id_nodo)
        nodo.executar()
        
    except ValueError as e:
        print(f'ERRO: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
