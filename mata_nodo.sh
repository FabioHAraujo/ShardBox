#!/bin/bash
# Script para matar nodo(s)

if [ -z "$1" ]; then
    echo "Uso: ./kill_node.sh <node_id>"
    echo "Exemplo: ./kill_node.sh 3  (mata o nodo 3), 0 mata todos os nodos"
    exit 1
fi

NODE_ID=$1

# Verifica se é número
if ! [[ "$NODE_ID" =~ ^[0-8]$ ]]; then
    echo "ERRO: node_id deve ser um número entre 0 e 8"
    exit 1
fi

if [ "$NODE_ID" -eq 0 ]; then
    echo "Matando todos os nodos..."
    pkill -f "node.py"
    if [ $? -eq 0 ]; then
        echo "Todos os nodos foram encerrados!"
    else
        echo "Nenhum processo encontrado."
    fi
else
    echo "Matando nodo $NODE_ID..."
    pkill -f "node.py $NODE_ID"
    if [ $? -eq 0 ]; then
        echo "Nodo $NODE_ID foi encerrado com sucesso!"
    else
        echo "Nenhum processo encontrado para o nodo $NODE_ID"
    fi
fi
