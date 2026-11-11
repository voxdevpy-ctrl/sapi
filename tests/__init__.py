"""Os testes usam um diretório de produção temporário e isolado (nunca os dados reais)."""
import os
import tempfile

os.environ["CORTES_PRODUCAO"] = tempfile.mkdtemp(prefix="cortes_teste_")
os.environ["SAPI_DADOS"] = os.environ["CORTES_PRODUCAO"]  # nunca deixa um SAPI_DADOS do sistema apontar para dados reais
os.environ.pop("CORTES_FAKE_NOW", None)
