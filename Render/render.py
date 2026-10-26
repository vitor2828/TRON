import os
import subprocess

def limpar_tela():
    subprocess.run("cls" if os.name=="nt" else "clear")

