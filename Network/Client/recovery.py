"""Acordo de recuperacao usado por GameSession (uma unica thread).

"""

from copy import deepcopy
import json

from . import protocolo as p


class RecoveryMixin:
    # 1. Evidencias locais: tres checkpoints e certificado de cada ciclo.
    def _init_recovery(self):
        for key, default in (("recovery_timeout_ms", 15000), ("probe_timeout_ms", 1500)):
            value = self.config.get(key, default)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} deve ser inteiro positivo")
        self._history = {}
        self._detection = None
        self._verified_failures = set()
        self._recovery = None
        self._disconnected = set()
        self._future_messages = []
        self.recovery_log = []
        self._save_checkpoint(None, self.active_participants, {}, {})

    def _state_digest(self, snapshot, members, epoch=None):
        return p.calculate_hash({
            "epoch": self.epoch if epoch is None else epoch,
            "snapshot": snapshot, "active_participants": sorted(members),
            "coordinator_id": min(members, default=None),
        })

    def _save_checkpoint(self, previous_hash, old_members, declarations, hashes):
        record = dict(
            tick=self.confirmed_tick, epoch=self.epoch,
            state_hash=self._state_digest(self._confirmed, self.active_participants),
            previous_hash=previous_hash, old_members=sorted(old_members),
            members=sorted(self.active_participants),
            declarations=[dict(sender_id=i, type=kind, data=deepcopy(data))
                          for i, (kind, data) in sorted(declarations.items())],
            hashes=[dict(sender_id=i, state_hash=digest) for i, digest in sorted(hashes.items())],
            snapshot=deepcopy(self._confirmed),
        )
        self._history[self.confirmed_tick] = record
        for tick in sorted(self._history)[:-3]:
            del self._history[tick]

    def _emit(self, message):
        message = deepcopy(message)
        if message["type"] not in p.RECOVERY_TYPES:
            message["epoch"] = self.epoch
        p.validate_message(message)
        self._outgoing.append(message)

    def _control(self, kind, **data):
        r = self._recovery
        message = p.make_recovery(kind, self.match_id, self.player_id,
                                  self.confirmed_tick, r["epoch"], r["id"], r["failed"], **data)
        self._emit(message)

    # 2. A API publica mantem o envio separado da sessao.
    def recipients_for(self, message):
        kind, data = message["type"], message["data"]
        if kind in {p.RECOVERY_PROBE, p.RECOVERY_ALIVE}:
            return frozenset({data["recipient_id"]})
        r = self._recovery
        if kind == p.FAILURE_REPORT:
            return frozenset({self.coordinator_id}) - {self.player_id}
        if kind in {p.RECOVERY_ACK, p.RECOVERY_APPLIED}:
            return frozenset({self.coordinator_id}) - {self.player_id}
        if kind in p.RECOVERY_TYPES:
            # A proposta contem os destinatarios; os outros controles pertencem
            # ao acordo corrente. O alvo nunca e usado para confirmar sua queda.
            members = data.get("members", r["members"] if r else self.active_participants)
            return frozenset(members) - {self.player_id, data["failed_id"]}
        if kind == p.ABORT and r:
            return r["members"] - {self.player_id, r["failed"]}
        return self._normal_recipients_for(message)

    def handle_message(self, sender, message):
        try:
            p.validate_origin(message, self.match_id, self.participants, sender)
            if sender == self.player_id:
                raise p.ProtocolError("Mensagem do proprio jogador")
            kind, epoch = message["type"], message["epoch"]
            # Uma sondagem permite distinguir timeout de um canal parcial. A
            # resposta negativa impede remover um processo ainda responsivo.
            if kind == p.RECOVERY_PROBE:
                data = message["data"]
                if (epoch == self.epoch and data["failed_id"] == self.player_id
                        and data["recipient_id"] == self.player_id):
                    self._emit(p.make_recovery(
                        p.RECOVERY_ALIVE, self.match_id, self.player_id,
                        self.confirmed_tick, epoch, data["recovery_id"], self.player_id,
                        recipient_id=sender))
                return
            if kind == p.RECOVERY_ALIVE and self._detection:
                data = message["data"]
                d = self._detection
                if (epoch == self.epoch and data["recipient_id"] == self.player_id
                        and sender in d["candidates"] and data["failed_id"] == sender
                        and data["recovery_id"] == f"{self.epoch + 1}:{sender}"):
                    d["alive"].add(sender)
                    return
            if (kind == p.FINISH_COMMIT and epoch == self.epoch
                    and self._confirmed["result"] is not None
                    and sender in self.active_participants
                    and message["tick"] == self.confirmed_tick
                    and message["data"]["state_hash"] == self._finish_digest
                    and message["data"]["ack_ids"] == sorted(self.active_participants)
                    and self.phase != "ABORTED"):
                # Pode cruzar FAILURE_REPORT em outro canal. O certificado prova
                # que todos ja confirmaram o resultado, encerrando a recuperacao.
                if self.phase != "FINISHED":
                    # Replica uma vez o certificado: o emissor pode cair antes
                    # de entrega-lo a todos os demais canais.
                    self._emit(p.make_message(p.FINISH_COMMIT, self.match_id,
                                              self.player_id, self.confirmed_tick,
                                              message["data"], epoch=self.epoch))
                self._recovery = None
                self._detection = None
                self.phase = "FINISHED"
                return
            if self.phase in {"LEFT", "FINISHED", "ABORTED"}:
                return
            r = self._recovery
            if kind == p.ABORT:
                allowed_epoch = epoch == self.epoch or (r and epoch == r["epoch"])
                if allowed_epoch and sender in self.active_participants:
                    self._abort(message["data"]["reason"], "ABORT remoto",
                                message["data"]["related_tick"], notify=False, sender_id=sender)
                return
            if kind in p.RECOVERY_TYPES:
                self._handle_control(sender, message)
                return
            if epoch < self.epoch:
                return
            if r:
                # Preserva mensagens da nova epoca que cruzem o RESUME de outro
                # canal. Declaracoes da epoca interrompida sao descartadas.
                if epoch == r["epoch"] + 1:
                    if len(self._future_messages) >= 128:
                        raise p.ProtocolError("Fila da nova epoca excedida")
                    self._future_messages.append((sender, deepcopy(message)))
                return
            if epoch != self.epoch:
                raise p.ProtocolError("Epoca futura sem recuperacao")
            self._handle_normal_message(sender, message)
        except (p.ProtocolError, ValueError, KeyError, TypeError) as error:
            self._abort("INVALID_MESSAGE", str(error), self.current_tick)

    # 3. Detectar nao equivale a remover. Sem coordenador, nao ha eleicao.
    def _handle_disconnect(self, player_id):
        self._disconnected.add(player_id)
        if self.phase in {"LEFT", "FINISHED", "ABORTED"} or player_id == self.player_id:
            return
        if player_id not in self.active_participants:
            return
        tick = self.current_tick
        declaration = self._declarations.get(tick, {}).get(player_id)
        if (not self._recovery and declaration and declaration[0] == p.LEAVE_REQUEST
                and player_id in self._hashes.get(tick, {})):
            return  # Sua ultima declaracao e hash ja chegaram.
        self._suspect(player_id)

    def _begin_detection(self, candidates):
        """Um hash ausente pode ser consequencia de outro MOVE ausente.

        Sonda todos os ausentes antes de concluir que houve varias quedas.
        """
        self._detection = dict(candidates=set(candidates), alive=set())
        self.phase = "FAILURE_CHECK"
        self._deadline = self._clock() + self.config.get("probe_timeout_ms", 1500) / 1000
        for candidate in sorted(candidates):
            self._emit(p.make_recovery(
                p.RECOVERY_PROBE, self.match_id, self.player_id, self.confirmed_tick,
                self.epoch, f"{self.epoch + 1}:{candidate}", candidate,
                recipient_id=candidate))

    def _drive_detection(self):
        if self._clock() < self._deadline:
            return
        d = self._detection
        missing = d["candidates"] - d["alive"]
        if len(missing) == 1:
            failed = next(iter(missing))
            self._verified_failures.add(failed)
            self._suspect(failed)
        else:
            self._abort("RECOVERY_FAILED", "Sondagem sem falha unica identificavel", self.current_tick)

    def _pause(self, failed, begun=False):
        self._detection = None
        self._outgoing.clear()  # Nao envia propostas antigas apos a pausa.
        self._recovery = dict(
            id=f"{self.epoch + 1}:{failed}", epoch=self.epoch, failed=failed,
            members=self.active_participants, begun=begun,
            chunks={}, totals={}, statuses={}, sent_status=False,
            acks=set(), applied=set(), proposal=None, pending_proposal=None,
            sent_probe=False, probe_deadline=None,
        )
        self._recovery_phase("RECOVERY_COLLECT" if begun else "RECOVERY_WAIT")

    def _recovery_phase(self, phase):
        self.phase = phase
        self._deadline = self._clock() + self.config.get("recovery_timeout_ms", 15000) / 1000

    def _suspect(self, failed):
        if failed == self.coordinator_id:
            self._abort("COORDINATOR_LOST", "Coordenador indisponivel", self.current_tick)
            return
        if self.phase in {"CONNECTING", "PREPARING", "WAITING_LEAVE_ACK"}:
            self._abort("RECOVERY_FAILED", "Falha fora da fase recuperavel", self.current_tick)
            return
        if self._recovery:
            if failed != self._recovery["failed"]:
                self._abort("RECOVERY_FAILED", "Segunda falha durante recuperacao", self.current_tick)
            elif self.player_id == self.coordinator_id and not self._recovery["begun"]:
                self._begin_recovery()
            return
        self._pause(failed)
        if self.player_id == self.coordinator_id:
            self._begin_recovery()
        else:
            self._control(p.FAILURE_REPORT)

    def _begin_recovery(self):
        r = self._recovery
        r["begun"] = True
        self._recovery_phase("RECOVERY_COLLECT")
        self._control(p.RECOVERY_BEGIN, members=sorted(r["members"]),
                      coordinator_id=self.coordinator_id)

    def _handle_control(self, sender, message):
        kind, data = message["type"], message["data"]
        r = self._recovery
        if message["epoch"] < (r["epoch"] if r else self.epoch):
            return
        if message["epoch"] != (r["epoch"] if r else self.epoch):
            raise p.ProtocolError("Controle de epoca futura")
        failed = data["failed_id"]
        expected_id = f"{message['epoch'] + 1}:{failed}"
        if data["recovery_id"] != expected_id:
            raise p.ProtocolError("Identificador de recuperacao invalido")
        if kind == p.FAILURE_REPORT:
            if self.player_id != self.coordinator_id or sender not in self.active_participants:
                raise p.ProtocolError("Relato fora do coordenador")
            if failed == sender or failed not in self.active_participants:
                raise p.ProtocolError("Suspeita invalida")
            self._suspect(failed)
            return
        if kind == p.RECOVERY_BEGIN:
            if sender != self.coordinator_id or data["coordinator_id"] != sender:
                raise p.ProtocolError("BEGIN fora do coordenador")
            if (frozenset(data["members"]) != self.active_participants or
                    failed not in self.active_participants or failed in {sender, self.player_id}):
                raise p.ProtocolError("Membros de recuperacao inconsistentes")
            if not r:
                self._pause(failed, begun=True)
                r = self._recovery
            elif r["failed"] != failed:
                raise p.ProtocolError("Duas falhas concorrentes")
            if not r["begun"]:
                r["begun"] = True
                self._recovery_phase("RECOVERY_COLLECT")
            return
        if not r and kind == p.RECOVERY_STATUS:
            # Outro canal pode entregar um STATUS antes do BEGIN do coordenador.
            if failed not in self.active_participants or failed in {self.player_id, self.coordinator_id}:
                raise p.ProtocolError("STATUS antecipado invalido")
            self._pause(failed)
            r = self._recovery
        if not r or r["id"] != data["recovery_id"]:
            raise p.ProtocolError("Controle sem BEGIN")
        if kind == p.RECOVERY_ALIVE:
            if sender == r["failed"] and data["recipient_id"] == self.player_id:
                self._abort("RECOVERY_FAILED", "Alvo respondeu a sondagem: suspeita nao unanime",
                            self.current_tick)
            return
        survivors = r["members"] - {failed}
        if sender not in survivors:
            raise p.ProtocolError("Controle de membro fora do acordo")
        if kind == p.RECOVERY_STATUS:
            self._receive_status(sender, data)
        elif kind == p.RECOVERY_PROPOSE:
            if sender != self.coordinator_id:
                raise p.ProtocolError("Proposta fora do coordenador")
            if r["pending_proposal"] is not None and r["pending_proposal"] != data:
                raise p.ProtocolError("Propostas contraditorias")
            r["pending_proposal"] = deepcopy(data)
        elif kind == p.RECOVERY_ACK:
            self._check_control_digest(data)
            if self.player_id != self.coordinator_id or self.phase not in {"RECOVERY_ACK", "RECOVERY_APPLY"}:
                raise p.ProtocolError("ACK inesperado")
            r["acks"].add(sender)
        elif kind == p.RECOVERY_COMMIT:
            if sender != self.coordinator_id or self.phase != "RECOVERY_ACK":
                raise p.ProtocolError("COMMIT inesperado")
            self._check_control_digest(data)
            self._apply_recovery()
        elif kind == p.RECOVERY_APPLIED:
            self._check_control_digest(data)
            if self.player_id != self.coordinator_id or self.phase != "RECOVERY_APPLY":
                raise p.ProtocolError("APPLIED inesperado")
            r["applied"].add(sender)
        elif kind == p.RECOVERY_RESUME:
            if sender != self.coordinator_id or self.phase != "RECOVERY_APPLY":
                raise p.ProtocolError("RESUME inesperado")
            self._check_control_digest(data)
            self._resume_recovery()
        else:
            raise p.ProtocolError("Controle inesperado")

    # 4. Status e fragmentado: nenhum snapshot completo percorre a rede.
    def _status(self):
        records = []
        for record in self._history.values():
            records.append({key: deepcopy(value) for key, value in record.items() if key != "snapshot"})
        return dict(confirmed_tick=self.confirmed_tick,
                    members=sorted(self.active_participants), coordinator_id=self.coordinator_id,
                    records=records)

    def _publish_status(self):
        r = self._recovery
        status = self._status()
        r["statuses"][self.player_id] = status
        content = p.canonical_bytes(status).decode("utf-8")
        parts = [content[i:i + 1000] for i in range(0, len(content), 1000)]
        if len(parts) > 64:
            raise p.ProtocolError("Status excedeu o limite de partes")
        for index, part in enumerate(parts):
            self._control(p.RECOVERY_STATUS, part=index, total=len(parts), content=part)
        r["sent_status"] = True

    def _receive_status(self, sender, data):
        r = self._recovery
        previous_total = r["totals"].setdefault(sender, data["total"])
        if previous_total != data["total"]:
            raise p.ProtocolError("Total de fragmentos contraditorio")
        chunks = r["chunks"].setdefault(sender, {})
        if data["part"] in chunks and chunks[data["part"]] != data["content"]:
            raise p.ProtocolError("Fragmento contraditorio")
        chunks[data["part"]] = data["content"]
        if len(chunks) == data["total"]:
            content = "".join(chunks[i] for i in range(data["total"]))
            try:
                status = json.loads(content, object_pairs_hook=p._unique_object)
            except (ValueError, RecursionError) as error:
                raise p.ProtocolError("Status JSON invalido") from error
            self._validate_status(status)
            r["statuses"][sender] = status

    def _validate_status(self, status):
        p.canonical_bytes(status)
        p._check_fields(status, {"confirmed_tick", "members", "coordinator_id", "records"}, "status")
        p._check_integer(status["confirmed_tick"], "confirmed_tick", 0)
        p._check_integer(status["coordinator_id"], "coordinator_id", 0)
        if (type(status["members"]) is not list or
                any(type(i) is not int for i in status["members"])):
            raise p.ProtocolError("members invalido no status")
        r = self._recovery
        if status["members"] != sorted(r["members"]) or status["coordinator_id"] != self.coordinator_id:
            raise p.ProtocolError("Composicao divergiu durante recuperacao")
        records = status["records"]
        if type(records) is not list or not 1 <= len(records) <= 3:
            raise p.ProtocolError("Historico invalido")
        ticks = []
        for record in records:
            p._check_fields(record, {"tick", "epoch", "state_hash", "previous_hash", "old_members",
                                     "members", "declarations", "hashes"}, "checkpoint")
            p._check_integer(record["tick"], "tick", 0)
            p._check_integer(record["epoch"], "epoch", 0)
            p._check_hash(record["state_hash"], "state_hash")
            if record["previous_hash"] is not None:
                p._check_hash(record["previous_hash"], "previous_hash")
            for key in ("old_members", "members"):
                ids = record[key]
                if (type(ids) is not list or any(type(i) is not int or i not in self.participants for i in ids)
                        or ids != sorted(set(ids))):
                    raise p.ProtocolError("IDs invalidos no checkpoint")
            if type(record["declarations"]) is not list or len(record["declarations"]) > 4:
                raise p.ProtocolError("Declaracoes invalidas no checkpoint")
            if type(record["hashes"]) is not list or len(record["hashes"]) > 4:
                raise p.ProtocolError("Hashes invalidos no checkpoint")
            ticks.append(record["tick"])
        if ticks != sorted(set(ticks)) or ticks[-1] != status["confirmed_tick"]:
            raise p.ProtocolError("Ciclos invalidos no historico")

    # 5. Preserva o maior ciclo JA confirmado, sem confiar em snapshot remoto.
    def _recovery_base(self):
        r = self._recovery
        statuses = r["statuses"]
        ticks = [s["confirmed_tick"] for s in statuses.values()]
        target = max(ticks)
        if target - min(ticks) > 1:
            raise p.ProtocolError("Distancia de ciclos excedeu a barreira lockstep")
        records = [record for status in statuses.values() for record in status["records"]
                   if record["tick"] == target]
        if not records or any(record != records[0] for record in records):
            raise p.ProtocolError("Evidencias de checkpoint divergentes")
        proof = records[0]
        if proof["epoch"] != r["epoch"] or proof["members"] != sorted(r["members"]):
            raise p.ProtocolError("Base pertence a outra composicao ou epoca")
        if self.confirmed_tick == target:
            base = deepcopy(self._confirmed)
        elif self.confirmed_tick + 1 == target:
            local_hash = self._history[self.confirmed_tick]["state_hash"]
            if proof["previous_hash"] != local_hash or proof["old_members"] != sorted(self.active_participants):
                raise p.ProtocolError("Checkpoint nao prolonga o estado local")
            declarations = {}
            for item in proof["declarations"]:
                p._check_fields(item, {"sender_id", "type", "data"}, "declaracao")
                sender = item["sender_id"]
                if type(sender) is not int or sender in declarations:
                    raise p.ProtocolError("Remetente de declaracao invalido")
                msg = p.make_message(item["type"], self.match_id, sender, target,
                                     item["data"], epoch=r["epoch"])
                if msg["type"] not in {p.MOVE, p.IDLE, p.LEAVE_REQUEST}:
                    raise p.ProtocolError("Tipo de declaracao invalido")
                declarations[sender] = (msg["type"], msg["data"])
            if set(declarations) != self.active_participants:
                raise p.ProtocolError("Certificado sem todas as declaracoes")
            moves, leaving = {}, set()
            for sender, (kind, data) in declarations.items():
                if kind == p.MOVE:
                    self._validate_move(sender, data)
                    moves[sender] = data
                elif kind == p.LEAVE_REQUEST:
                    leaving.add(sender)
                elif next(player["alive"] for player in self._confirmed["players"] if player["id"] == sender):
                    raise p.ProtocolError("Moto viva com IDLE no certificado")
            alive = {player["id"] for player in self._confirmed["players"] if player["alive"]}
            if set(moves) != alive - leaving or leaving:
                raise p.ProtocolError("Remocao voluntaria parcialmente confirmada: nao recuperavel")
            hashes = {}
            for item in proof["hashes"]:
                p._check_fields(item, {"sender_id", "state_hash"}, "hash do certificado")
                sender = item["sender_id"]
                if type(sender) is not int or sender in hashes:
                    raise p.ProtocolError("Remetente de hash invalido")
                p._check_hash(item["state_hash"], "state_hash")
                hashes[sender] = item["state_hash"]
            if set(hashes) != self.active_participants or any(d != proof["state_hash"] for d in hashes.values()):
                raise p.ProtocolError("Certificado sem todos os hashes iguais")
            base = self._normalize_snapshot(self._simulate_departures(moves, set()), target)
        else:
            raise p.ProtocolError("Base fora da janela local")
        digest = self._state_digest(base, r["members"], epoch=r["epoch"])
        if digest != proof["state_hash"]:
            raise p.ProtocolError("Nao foi possivel reproduzir o checkpoint")
        return base, digest

    def _make_proposal(self):
        r = self._recovery
        base, base_hash = self._recovery_base()
        result = deepcopy(base)
        if result["result"] is None:
            for player in result["players"]:
                if player["id"] == r["failed"]:
                    player["alive"] = False
            alive = [player["id"] for player in result["players"] if player["alive"]]
            result["result"] = None if len(alive) > 1 else {"winner_id": alive[0] if alive else None}
        # Um resultado ja confirmado nao e reescrito por uma queda posterior.
        # Nesse caso apenas concluimos a entrega final aos sobreviventes.
        result = self._normalize_snapshot(result, base["tick"])
        members = r["members"] - {r["failed"]}
        new_epoch = r["epoch"] + 1
        digest = self._state_digest(result, members, new_epoch)
        r["result"] = result
        return dict(recovery_id=r["id"], failed_id=r["failed"],
                    base_tick=base["tick"], base_hash=base_hash, state_hash=digest,
                    members=sorted(members), coordinator_id=self.coordinator_id,
                    new_epoch=new_epoch)

    def _check_control_digest(self, data):
        proposal = self._recovery["proposal"]
        if not proposal or data["state_hash"] != proposal["state_hash"]:
            raise p.ProtocolError("Hash da decisao de recuperacao divergiu")

    # 6. ACK -> COMMIT -> APPLIED -> RESUME. Nenhum peer retoma por timeout.
    def _drive_recovery(self):
        r = self._recovery
        if not r or self.phase in {"LEFT", "FINISHED", "ABORTED"}:
            return
        try:
            if self._clock() >= self._deadline:
                self._abort("RECOVERY_FAILED", "Prazo do acordo de recuperacao excedido", self.current_tick)
                return
            if not r["begun"]:
                return
            if not r["sent_status"]:
                if r["failed"] in self._disconnected or r["failed"] in self._verified_failures:
                    self._publish_status()
                elif not r["sent_probe"]:
                    self._control(p.RECOVERY_PROBE, recipient_id=r["failed"])
                    r["sent_probe"] = True
                    r["probe_deadline"] = self._clock() + self.config.get("probe_timeout_ms", 1500) / 1000
                elif self._clock() >= r["probe_deadline"]:
                    self._publish_status()
            survivors = r["members"] - {r["failed"]}
            if self.phase == "RECOVERY_COLLECT" and set(r["statuses"]) == survivors:
                expected = self._make_proposal()
                if self.player_id == self.coordinator_id:
                    r["proposal"] = expected
                    r["acks"].add(self.player_id)
                    self._recovery_phase("RECOVERY_ACK")
                    self._control(p.RECOVERY_PROPOSE, **{k: v for k, v in expected.items()
                                                       if k not in {"recovery_id", "failed_id"}})
                elif r["pending_proposal"] is not None:
                    if expected != r["pending_proposal"]:
                        raise p.ProtocolError("Proposta nao corresponde aos relatos")
                    r["proposal"] = expected
                    self._recovery_phase("RECOVERY_ACK")
                    self._control(p.RECOVERY_ACK, state_hash=expected["state_hash"])
            if (self.player_id == self.coordinator_id and self.phase == "RECOVERY_ACK"
                    and r["acks"] == survivors):
                self._control(p.RECOVERY_COMMIT, state_hash=r["proposal"]["state_hash"])
                self._apply_recovery()
            if (self.player_id == self.coordinator_id and self.phase == "RECOVERY_APPLY"
                    and r["applied"] == survivors):
                self._control(p.RECOVERY_RESUME, state_hash=r["proposal"]["state_hash"])
                self._resume_recovery()
        except (p.ProtocolError, ValueError, KeyError, TypeError) as error:
            self._abort("RECOVERY_FAILED", str(error), self.current_tick)

    def _apply_recovery(self):
        r = self._recovery
        self._confirmed = deepcopy(r["result"])
        self.confirmed_tick = self._confirmed["tick"]
        self.epoch = r["proposal"]["new_epoch"]
        self.active_participants = frozenset(r["proposal"]["members"])
        self.coordinator_id = min(self.active_participants)
        self._candidate = None
        self._moves.clear()
        self._declarations.clear()
        self._hashes.clear()
        self._round_members.clear()
        self._leave_acks.clear()
        self._finish_acks.clear()
        self._finish_digest = None
        self._history.clear()
        self._save_checkpoint(None, self.active_participants, {}, {})
        self._recovery_phase("RECOVERY_APPLY")
        if self.player_id == self.coordinator_id:
            r["applied"].add(self.player_id)
        else:
            self._control(p.RECOVERY_APPLIED, state_hash=r["proposal"]["state_hash"])

    def _resume_recovery(self):
        r = self._recovery
        self.recovery_log.append(deepcopy(r["proposal"]))
        self._recovery = None
        pending, self._future_messages = self._future_messages, []
        if self._confirmed["result"] is not None:
            self.phase = "FINISHED"
        else:
            self._enter_moves()
        for sender, message in pending:
            self.handle_message(sender, message)
