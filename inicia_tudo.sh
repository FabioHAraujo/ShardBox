#!/bin/bash
# Script para iniciar todos os 8 nodos

echo "Iniciando todos os 8 nodos..."

for i in {1..8}; do
    echo "Iniciando nodo $i"
    ./venv/bin/python3 node.py $i &
    sleep 1  # 1 segundo entre cada nodo para evitar conflitos de porta
done

echo "Todos os nodos foram iniciados!"
echo "Use 'pkill -f node.py' para parar todos os nodos"
echo "Use 'tail -f log/nodo_*.log' para ver os logs"

# Mantém o script rodando
wait
