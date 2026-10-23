# TrustHarbor — end-to-end report

- **Status:** PASSED
- **Mode:** live  |  **Network:** studionet (chain id 61999)  |  **RPC:** https://studio.genlayer.com/api
- **Started:** 2026-10-23T05:21:36.878Z  |  **Finished:** 2026-10-23T06:14:05.039Z
- **Totals:** 164 passed · 0 failed · 8 warnings · 0 skipped
- **ArbitrationBench:** `0x61eDa77e6E12708375869ff5A1d36A055fD624A1`
- **DealEscrow:** `0x55F3198686d6AFBff674fa6F051a374B9A5A756C`
- **MerchantBond:** `0xa74e88F6381D442fb981e1F60c1C2430e34c99BD`

## S0 — Setup: accounts, funding, deployment and wiring

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | account owner created and funded | 0x460012C6e798a520b115B61eD7DdEaef0289BB76 with 1 GEN |  |
| PASS | account buyer created and funded | 0x574c634d3C9f421053fD5468D114EB581b7deE6B with 5 GEN |  |
| PASS | account seller created and funded | 0x5d03F6E73a6C05452d79aA04963b2C366955D4AD with 3 GEN |  |
| PASS | account seller2 created and funded | 0xD4523348d6172316dda40774B47ea30EDa266F28 with 2 GEN |  |
| PASS | account outsider created and funded | 0xe029295595e9c1CEAaa80C86e6180CA0481c457d with 1 GEN |  |
| PASS | ArbitrationBench deployed | 0x61eDa77e6E12708375869ff5A1d36A055fD624A1 | `0xf08477bbf713283881a64c27516bdbf434dad28bdd9e521d00f46b1febe7e3dc` |
| PASS | DealEscrow deployed | 0x55F3198686d6AFBff674fa6F051a374B9A5A756C | `0xe5e8f7b06b0fe6baaac60a7bdf6545e1a9ad66506ee205dac149b6e869208670` |
| PASS | MerchantBond deployed | 0xa74e88F6381D442fb981e1F60c1C2430e34c99BD | `0x310d0d16f33892d12cda3f78547fc0b4eb705073bcaaf82651f4be2ff7791037` |
| PASS | each contract is owned by the deployer |  |  |
| PASS | DealEscrow is wired to the bench | 0x61eDa77e6E12708375869ff5A1d36A055fD624A1 |  |
| PASS | MerchantBond is wired to the escrow | 0x55F3198686d6AFBff674fa6F051a374B9A5A756C |  |
| PASS | constructor parameters were applied | appeal 150s, fee 250bps, review 900s, penalty 2000bps, unbond 60s |  |
| PASS | fresh deployment: escrow holds at least what it owes | balance 0 GEN ≥ owed 0 GEN + fees 0 GEN |  |
| PASS | fresh deployment: bond contract holds at least the bonds | balance 0 GEN ≥ bonded 0 GEN |  |
| PASS | fresh deployment: the bench never holds value | balance 0 wei (only 0 wei of refused-call dust allowed) |  |

## S1 — An undisputed deal: fund → deliver → confirm → withdraw

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | buyer funds a 0.01 GEN deal | buyer → escrow.open_deal | `0x536bd9f9ba4291c98bd11c4dba40db253242f6e742a9c9a83484f0f2bd90831f` |
| PASS | the new deal is FUNDED with the exact price | deal 1: FUNDED, price 0.01 GEN |  |
| PASS | the price is escrowed (owed_total grew by exactly the price) | owed 0 GEN → 0.01 GEN |  |
| PASS | after funding: escrow holds at least what it owes | balance 0.01 GEN ≥ owed 0.01 GEN + fees 0 GEN |  |
| PASS | after funding: bond contract holds at least the bonds | balance 0 GEN ≥ bonded 0 GEN |  |
| PASS | after funding: the bench never holds value | balance 0 wei (only 0 wei of refused-call dust allowed) |  |
| PASS | seller declares delivery | seller → escrow.mark_delivered | `0xb8bbaa778c392b3e7e345054db6a587be033d3c5892777485c652d3c268ebd87` |
| PASS | the deal is DELIVERED and the review clock runs |  |  |
| PASS | buyer confirms receipt | buyer → escrow.confirm_receipt | `0x41628852a394cc9acd8ec371e4f96fa31da9f9fb8f1002ff4c839ba6bfdfc51f` |
| PASS | the deal is RELEASED to the seller minus the platform fee | seller 0.00975 GEN, fee 0.00025 GEN |  |
| PASS | seller withdraws 0.00975 GEN | seller → escrow.withdraw | `0x39fd938484ed4c64d1d2ff417b894ca308712c55e5d1b946fcd107d8f7ac04db` |
| PASS | seller's claimable balance is zero after withdrawing | left 0 GEN |  |
| WARN | seller receives the payout | seller balance is 3 GEN, expected about 3.00975 GEN (payout not finalized yet?) |  |
| PASS | after the first payout: balances equal the books exactly | escrow 0.00025 GEN, bond contract 0 GEN |  |

## S2 — A silent buyer: auto-release after the review window

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | owner shortens the review window for NEW deals to 60 s | owner → escrow.set_review_window | `0x84a07c277ac68909a6a75d49d66dc13f5a648be696dc963d18f3e4890f17e4ba` |
| PASS | buyer funds a deal that will go silent | buyer → escrow.open_deal | `0x35d695be70b0138d825cb0f64cbb78042f71098873bb2875b8b8b2f417ec8fb5` |
| PASS | the new deal is FUNDED with the exact price | deal 2: FUNDED, price 0.01 GEN |  |
| PASS | seller declares delivery | seller → escrow.mark_delivered | `0xc14b298261c9c3dafa8ffa65156f467a49009608966e882c491dc628a0ef2b26` |
| PASS | the deal copied the 60 s review window |  |  |
| PASS | nobody can release inside the review window | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · the buyer's review window is still open) | `0x34b61027191861c558aca38643e8bea9af88bed40132644eeb050d9a722430c3` |
| INFO | waiting 65s | the buyer stays silent past the review window |  |
| PASS | a stranger releases the deal after the window | outsider → escrow.release_after_review | `0x9209b842cb7b5084b49d4d8b8746a2e439e538f0660f94a319e27ae8fcd25cf7` |
| PASS | the resolution is AUTO_RELEASE | AUTO_RELEASE |  |
| PASS | seller withdraws 0.00975 GEN | seller → escrow.withdraw | `0xdd74fd971610d174feb1636ae525e9476d474fc5441c438c0058cdc92ff5d6d8` |
| PASS | seller's claimable balance is zero after withdrawing | left 0 GEN |  |
| WARN | seller receives the payout | seller balance is 3 GEN, expected about 3.00975 GEN (payout not finalized yet?) |  |
| PASS | owner restores the review window | owner → escrow.set_review_window | `0x06dc78622c78ffe53c46a69cb129b77250a73576f7cc8b377602c6e3cbc23317` |

## S3 — A seller who never delivers: full refund after the deadline

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | buyer funds a deal with a 60 s delivery window | buyer → escrow.open_deal | `0xbd924ca03863436a52b8e8b4f3c827760050feebfe76a7a02cba39e567b3f654` |
| PASS | the new deal is FUNDED with the exact price | deal 3: FUNDED, price 0.01 GEN |  |
| PASS | the buyer cannot reclaim before the deadline | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · the delivery deadline has not passed yet) | `0xaeb10ed37b7cce5c16964e6ceb40c7578b6b3d00837f9b3eaafb5e82f4ad4a4f` |
| INFO | waiting 65s | the delivery deadline passes |  |
| PASS | the seller cannot deliver after the deadline | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · the delivery deadline has passed) | `0x53fb9a458b11d1bc46704443e694ed9452f005d54ff02c2aa9217bcf826f3034` |
| PASS | a stranger triggers the full refund | outsider → escrow.reclaim_undelivered | `0x00287603bf01a7f9e2eba1bdc7b39157637bc3a5a18c4531c376c93fa360b27a` |
| PASS | the whole price goes back to the buyer with no fee | REFUNDED, buyer 0.01 GEN |  |
| PASS | buyer withdraws 0.01 GEN | buyer → escrow.withdraw | `0x557d325081599744746885ad1bab24972ca36256c7f3a747df79d8a109504bf2` |
| PASS | buyer's claimable balance is zero after withdrawing | left 0 GEN |  |
| WARN | buyer receives the payout | buyer balance is 4.97 GEN, expected about 4.98 GEN (payout not finalized yet?) |  |

## S4 — The seller refunds the buyer voluntarily

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | buyer funds a deal the seller will refund | buyer → escrow.open_deal | `0xcf5817f465a812f4c2123ce407f558308ec493ca34780b217027adca57521255` |
| PASS | the new deal is FUNDED with the exact price | deal 4: FUNDED, price 0.01 GEN |  |
| PASS | seller refunds the buyer | seller → escrow.refund_buyer | `0x0a53143dae2b0c34ac4eca6e396cf314ba2e83815f3957998ee58b497ccda817` |
| PASS | the deal is REFUNDED in full | REFUNDED |  |
| PASS | a settled deal cannot be refunded twice | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · deal is REFUNDED, not FUNDED or DELIVERED) | `0xd40eeb386d43d750cedf4e058997e76e4992b1772a6d55931af836b335ce72fb` |
| PASS | buyer withdraws 0.01 GEN | buyer → escrow.withdraw | `0x58862f808bbaebb181f2bf797e5ff76033ca39398b485a65419419e4d3bff8ff` |
| PASS | buyer's claimable balance is zero after withdrawing | left 0 GEN |  |
| WARN | buyer receives the payout | buyer balance is 4.96 GEN, expected about 4.97 GEN (payout not finalized yet?) |  |

## S5 — A disputed deal: live AI ruling, settlement, bond compensation

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | owner lowers the trust threshold to 0.01 GEN | owner → bond.set_trust_threshold | `0xb7ae1dc7ca98ea7cb4f3a571850d3225562e827ec53398ae674fc490adb5076d` |
| PASS | seller posts a 0.05 GEN bond | seller → bond.post_bond | `0x7bb5db1176191602474d544e6a1af58b9efc786f9bc520ffb48aa86d93ff0995` |
| PASS | the bond is recorded and the seller is trusted | bonded 0.05 GEN, trusted true |  |
| PASS | buyer funds a 0.05 GEN deal | buyer → escrow.open_deal | `0xcee88f0af486cf6146756448912052b05fb113619c38823aca38f0e6f08242a4` |
| PASS | the new deal is FUNDED with the exact price | deal 5: FUNDED, price 0.05 GEN |  |
| PASS | seller declares delivery | seller → escrow.mark_delivered | `0xd5a524a8609425f4c86ff8f686bd35543587566343d6c74afe29891d2fa0fd35` |
| PASS | buyer opens a case on the bench using the dispute template | buyer → bench.open_case | `0x91deb7e444ad5b5525c23e8f47f6f5a2a337a6da1818f391e02d5e07ea440a71` |
| PASS | buyer attaches the case to the deal | buyer → escrow.attach_case | `0x679b13dba250e61fed7fa88f51ad0340ef318054cd3ac22fd5015d2c454dc2a2` |
| PASS | the deal is DISPUTED and bound to that case | case 1 |  |
| PASS | the seller cannot refund their way out of a pending ruling | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · deal is DISPUTED, not FUNDED or DELIVERED) | `0xdbf65c14fafdcf7b137a99583d8b7d161b08cc9175d3b4e90271fc61a2370239` |
| PASS | a stranger cannot settle a deal that has no final ruling | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · the case is OPEN on the bench; settle after its FINAL ruling (or, for ) | `0x1d89a1446c9865a2fa2461791df2e6e9a5d371495ea41aefe893bd8a2752cce3` |
| PASS | the case cannot be ruled while the respondent still has time to answer | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · the respondent still has time to answer) | `0x7f87175acf4e933efa8061b3ca486cc094ca27cd7abdfa537c1ee26f611a2b73` |
| PASS | only the respondent can answer the case | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the respondent can file a response) | `0x8cb2c989095267f1df16e9ee1a8241352d5ad26fdc86d479471fcc4858f3e986` |
| PASS | a case that is not this deal's cannot be attached twice | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · deal is DISPUTED; a case can only be attached while it is DELIVERED) | `0xb3c57b7f8534b36aceb0f46ba98ca3c6e260b01a42452f3b743db5aca0776800` |
| PASS | seller files a response with their evidence | seller → bench.respond | `0xeac85b37e9eccad0d1e5b52a7c03f91d8b6b31aa92db2689d8357ed168616148` |
| PASS | a stranger asks the bench to rule (attempt 1) | outsider → bench.rule_case | `0x93ffe6ee1bfa559ecb1b315e74b759af0d7206060ec5de75db6b8ad4cffefd24` |
| INFO | the arbiter ruled | claimant (buyer) share 100% · confidence 99 · "Standard required a complete 4k logo pack in PNG and SVG. Seller admits delivering only one 200x200 JPG, so the promised deliverables were not provided." |  |
| PASS | the ruling is a legal value | share 100, confidence 99 |  |
| PASS | a ruled case cannot be ruled again | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · case is RULED, not open for a ruling) | `0xa75fcdac7ee731c67470922a8681cb04f21728a5fd06045c1a1b0111484a3a76` |
| PASS | the deal cannot be settled during the appeal window | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · the case is RULED on the bench; settle after its FINAL ruling (or, for) | `0xa4a3b73b39cf2f85d0bb0015b6ed43a969ce216982f9dfd23ca64333b3e2e160` |
| PASS | a ruled case cannot be withdrawn | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · case is RULED, not open) | `0x3b85d5b1aeb26a5ab3466dd347192aff9136f05bb21d90c075c473743117419a` |
| INFO | waiting 155s | the appeal window runs out and the ruling becomes final |  |
| PASS | a stranger settles the deal with the final ruling | outsider → escrow.settle_deal | `0x6db0fc7f8b27899426f0b9760669095cc7ac22ee07a1399d6618bbebf6d6100e` |
| PASS | the deal is SETTLED by the ruling | SETTLED/RULING |  |
| PASS | the split matches the ruled share exactly and loses no wei | buyer 0.05 GEN (100%), seller 0 GEN, fee 0 GEN |  |
| PASS | a deal cannot be settled twice | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · deal is SETTLED, not DISPUTED) | `0x1b2c07875e3ba207663d6213b2081b969ff26523bde6c51509b3d02d8f33d063` |
| PASS | the bond contract says the buyer may claim | payable 0.01 GEN |  |
| PASS | only the deal's buyer can claim compensation | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the deal's buyer can claim compensation) | `0x468d0fbb1e00bb4e93f199db751864c27136efc4564ed6f0b17525049db8e3f9` |
| PASS | the seller cannot claim on the buyer's behalf | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the deal's buyer can claim compensation) | `0xc63cc57a29b0da704d4f738ef8eb8e209548e2c093a0c8672143f014a687aff8` |
| PASS | the buyer claims compensation from the seller's bond | buyer → bond.claim_compensation | `0xfe3eb06fd4d5afff97beb003d124a79cf588fd2bf82dafa60c979c06c9fd9036` |
| PASS | exactly the penalty was slashed and the seller has one strike | bonded 0.04 GEN, strikes 1, slashed 0.01 GEN |  |
| PASS | a deal can only be claimed once | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · compensation for this deal was already claimed) | `0x84efd6324120967ecfec0f6cb2e73cfe0bccbb415b2a3945461e24f295d80023` |
| PASS | seller asks to unbond everything that is left | seller → bond.request_unbond | `0x5bc5448ab8af612c3f7fc5b08508e155f7c0646d3fc23b3f76e1664176f15e53` |
| INFO | waiting 65s | the unbonding delay passes |  |
| PASS | the seller still cannot walk away right after an adverse ruling | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · a buyer may still claim against your bond until 1792993665) | `0xe29ef2e3a3c5df77d5598a25966865e3d3db2c15fdecd15ce8b2d2148f7dc1b5` |
| PASS | seller cancels the unbond request | seller → bond.cancel_unbond | `0xa0a635718465dc03648689c85cc3040738686a5d125a1b4444c2a3c3fc3c8960` |
| PASS | buyer withdraws 0.05 GEN | buyer → escrow.withdraw | `0x1f6f0387b33c26bb5d799aa85fe15482d9a71a542c2da182ad80525c31c7c4bc` |
| PASS | buyer's claimable balance is zero after withdrawing | left 0 GEN |  |
| WARN | buyer receives the payout | buyer balance is 4.91 GEN, expected about 4.96 GEN (payout not finalized yet?) |  |
| PASS | after the dispute: escrow holds at least what it owes | balance 0.0005 GEN ≥ owed 0 GEN + fees 0.0005 GEN |  |
| PASS | after the dispute: bond contract holds at least the bonds | balance 0.04 GEN ≥ bonded 0.04 GEN |  |
| PASS | after the dispute: the bench never holds value | balance 0 wei (only 0 wei of refused-call dust allowed) |  |
| PASS | after the dispute payouts: balances equal the books exactly | escrow 0.0005 GEN, bond contract 0.04 GEN |  |

## S6 — Adversarial: hostile callers, inputs, ids and value

| | Step | Detail | Tx |
|---|---|---|---|
| INFO | open_deal with empty terms | transaction accepted, rejection returned in the result | `0x665a5187e9d135ac3886941c68aa6778cfb7711ed22dd582f8b7433671141a2b` |
| INFO | open_deal with over-long terms (301 chars) | transaction accepted, rejection returned in the result | `0x3de18277bf21738c4be702665d67b47d1f7b9618a2fe27d29b3ab93a5ebe0705` |
| INFO | open_deal with price below the minimum | transaction accepted, rejection returned in the result | `0x6eb0bcd8a2a30410b4bbe9a485660ffb591bf7d48dd0eacdbc9fc0a57c284700` |
| INFO | open_deal with seller is the buyer | transaction accepted, rejection returned in the result | `0xa690154e8b2d7c12675cc9bbe8788d96040712f840f2b652e08077a46b9eb96f` |
| INFO | open_deal with zero-address seller | transaction accepted, rejection returned in the result | `0x6bcb7362fe6e061edeccd231dbbe85764fbca69e8af4ae662d9f1642bfb9f9de` |
| INFO | open_deal with garbage seller | transaction accepted, rejection returned in the result | `0x314fd65e389f163bfc98e88d83bfe9b38b0d5ffc82766b4069aeeeb8afef61cb` |
| INFO | open_deal with delivery window of 1 second | transaction accepted, rejection returned in the result | `0x6f2c117074095f4c381c102bb709d898c37f2a91d0b85e949a9ef593aee3280d` |
| PASS | none of the hostile deals was created | deals 5 → 5 |  |
| PASS | dust bonds are not recorded | bonded 0.04 GEN |  |
| PASS | after refused deposits: escrow holds at least what it owes | balance 0.0205 GEN ≥ owed 0 GEN + fees 0.0005 GEN |  |
| PASS | after refused deposits: bond contract holds at least the bonds | balance 0.040000 GEN ≥ bonded 0.04 GEN |  |
| PASS | after refused deposits: the bench never holds value | balance 0 wei (only 0 wei of refused-call dust allowed) |  |
| PASS | after refused deposits (refunds): balances equal the books exactly | escrow 0.0005 GEN, bond contract 0.04 GEN |  |
| PASS | a stranger cannot call escrow.set_paused | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0x4839ece736ea75655ac3f28383231a7486b84533e79f4c87786165e9f78de604` |
| PASS | a stranger cannot call escrow.set_fee_bps | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0x4322ff5aa35ede288cf404ad382daf77c3cc47110f8a7fa0b42ba41737f13583` |
| PASS | a stranger cannot call escrow.set_review_window | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0x1560044500b71b7b8022b28e714c1f40834bfea961ef05cd5dfe4adec46db1fa` |
| PASS | a stranger cannot call escrow.withdraw_fees | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0x2402fde7b4c2d6193bd848bc302a2c6904a1a22fb43fdbda0db66f8ffde9f36c` |
| PASS | a stranger cannot call escrow.transfer_ownership | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0x0933829a5f8161dcbc5352b23a3484d1005bcf69534dd35ff10f456e29841a4e` |
| PASS | a stranger cannot call bench.set_paused | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0xea1e031386899b9d999432220417b660d9381581d05d2a69f4b1d7c60e9d3ef8` |
| PASS | a stranger cannot call bench.set_appeal_window | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0xc36825bf52fb6508e14ed921166ad3c8b7c5440a3dde02f692b1ed01095dff7a` |
| PASS | a stranger cannot call bench.transfer_ownership | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0x81fc91ccdc42e26e5c6c39cd7fef602ec5fa5c4d58d8cf8edf9eeb53b2700f61` |
| PASS | a stranger cannot call bond.set_paused | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0x92893403c88b19eb8ea749ce55b4a3ff638ad66bec6fd758b7f65f66e7c1259b` |
| PASS | a stranger cannot call bond.set_trust_threshold | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0xc39eb471a835e0caff59c079cf4dc8dae822f4adac0ae3cfe05bd25a7e3ef94a` |
| PASS | a stranger cannot call bond.transfer_ownership | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · caller is not the owner) | `0xbdc11668d124d9b49677f1995b1fc4413b9a911c2673c4731a620c0c55e6f0b0` |
| PASS | ownership and settings are unchanged |  |  |
| PASS | buyer funds a deal naming the seller in upper-case hex | buyer → escrow.open_deal | `0xd74487335db78af05da9c28eed4096d019697cde470144d797bce1b19abd2b3a` |
| PASS | the new deal is FUNDED with the exact price | deal 6: FUNDED, price 0.01 GEN |  |
| PASS | the seller address was normalised | 0x5d03F6E73a6C05452d79aA04963b2C366955D4AD |  |
| PASS | a stranger cannot declare delivery | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the seller can mark a deal delivered) | `0x23b5e5bd44729a04dc2fa60ba865d5c1195543ff311db2ea676d728bfb5884c3` |
| PASS | the buyer cannot declare delivery for the seller | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the seller can mark a deal delivered) | `0x3407442efe16803b3d019c04eb8a3717d7b90a6e1c61e1c0d70174782ef997d4` |
| PASS | a stranger cannot confirm receipt | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the buyer can confirm receipt) | `0x50b8a22602b32601f01ee1a7ebf60db2a42fc63d139a7212bb6b97ab2c116fcd` |
| PASS | the seller cannot confirm receipt for the buyer | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the buyer can confirm receipt) | `0xe5b4b2ec33a3bd92adfe44a089173b0b51affc5352b19ee86705ebeb874be5d1` |
| PASS | a stranger cannot refund the buyer | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the seller can refund the buyer) | `0xdcbf81fd17c5613ef5653d836d2a65b8c32cd1d7579ef81e20679d2023c2c490` |
| PASS | the seller (lower-case address) declares delivery | seller → escrow.mark_delivered | `0x8c8a0e37f6fe8deb28d8b30b565162a1d90340b542f67f30859102a2ba6163dc` |
| PASS | delivery cannot be declared twice | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · deal is DELIVERED, not FUNDED) | `0xdd91147526eff274488f8df02df680b2aab7a86605bcb3b821b996a3d4c5f093` |
| PASS | a stranger cannot attach a case to the deal | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · only the buyer or the seller can attach a case) | `0x603ff3b1568e6f3092274897e6e4538f3f8ee178e9718ddb8d1f9533d2df4b86` |
| PASS | a case that does not exist cannot be attached | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · case not found on the arbitration bench) | `0x6ec4d029a76f75e5407f1954bd367b99e40f1202a191960de823fbf72a12f8e1` |
| PASS | the buyer confirms and closes the deal | buyer → escrow.confirm_receipt | `0x8cc289e53b380258233064b427dffb388164bbed138862de58e6604b2aeaa050` |
| PASS | seller withdraws 0.00975 GEN | seller → escrow.withdraw | `0xb684d5d6df7d68a9e6e8ea66b909b243b3eeb91a3ed428d47b12d53271d0e824` |
| PASS | seller's claimable balance is zero after withdrawing | left 0 GEN |  |
| WARN | seller receives the payout | seller balance is 2.95 GEN, expected about 2.95975 GEN (payout not finalized yet?) |  |
| PASS | views answer "not found" for id -1 |  |  |
| PASS | views answer "not found" for id 4294967296 |  |  |
| PASS | views answer "not found" for id 1099511627776 |  |  |
| PASS | views answer "not found" for id 1180591620717411303424 |  |  |
| PASS | rule_case refuses an impossible id | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · unknown case_id) | `0xc07cb6a7c61e07c88da21dc03947280ca8c0eaf5a3ef5461b348221a0b864cc5` |
| PASS | settle_deal refuses a negative id | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · unknown deal_id) | `0x0fb88f0391e66472774f600ff19eac0eb2a3bf3077e60dfe9b222cff226ab826` |
| PASS | claim_compensation refuses an impossible id | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · unknown deal_id on DealEscrow) | `0x80a7fdf7e4620ce8fadf687fcf369306046f447a6948b30505430ccef3a3f151` |
| PASS | set_trust_threshold refuses a value beyond the storage width | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · threshold must be 0..1000000000000000000000000000000 wei) | `0x796037c5691e017892e13f978c81ca323602d2b5ff51dd877209af2f5c3c0f05` |
| PASS | value sent to a non-payable escrow method is refused | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=contract_error · exit_code 1) | `0x58f480d380329eaf1a1522a6181defe648a26a86c84a350674f9d04a4ecd84f2` |
| PASS | value sent to a non-payable bench method is refused | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=contract_error · exit_code 1) | `0xaef49430d638529d707b9d3468d77d90b9d82f68fd4ab2771dde9ca81750f365` |
| PASS | a refused payable-less call leaves the bench state untouched (at most the attached dust stays) | paused false, balance 1 wei, attached 1 wei |  |
| PASS | a stranger has nothing to withdraw | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · nothing to withdraw) | `0x49210f864e0ace624e3e710e8ea6b76e7a1b6d7cfb1acd75487d3fddc850972a` |
| PASS | a stranger has no bond to unbond | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · you have no bond) | `0x111d614a0f5e0309d740a01fceab852787bd0e3874ac489f40c02ea72a05e738` |
| PASS | a stranger cannot withdraw an unbond | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · no unbond request is pending) | `0x1583aee9e562a422868faf2b5be30499c33a068a544f26aa1ac14f176daf62ce` |
| PASS | a stranger opens a spam case naming the buyer (permissionless by design) | outsider → bench.open_case | `0x02a84fa05ea9ca056c45e233e73508ea15d7f936dbed102c37779095695ef221` |
| PASS | the stranger withdraws their own spam case | outsider → bench.withdraw_case | `0xb0e8690c9ea1ec37d14e40bb1afbfc1cd4761afc8216933eccf57d78a2735e06` |
| PASS | after the hostile round: escrow holds at least what it owes | balance 0.000750 GEN ≥ owed 0 GEN + fees 0.00075 GEN |  |
| PASS | after the hostile round: bond contract holds at least the bonds | balance 0.04 GEN ≥ bonded 0.04 GEN |  |
| PASS | after the hostile round: the bench never holds value | balance 1 wei (only 1 wei of refused-call dust allowed) |  |
| INFO | hostile round complete | every refusal above is an expected revert |  |

## S7 — A seller with a clean record unbonds and leaves

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | a second seller posts a 0.02 GEN bond | seller2 → bond.post_bond | `0xe14cd7eb66d12aa78501210b2500ba4fd9791584019d16337e18263f382092a2` |
| PASS | the second seller is trusted |  |  |
| PASS | they cannot unbond more than they have | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · amount must be 1..20000000000000000 wei) | `0x0931a195614fe9193692222c30aa503f8e8fd97b5495f88f84ff01e679735fce` |
| PASS | they request to unbond everything | seller2 → bond.request_unbond | `0x188544f8fd17a38127b23f6c4d9be105a20893012aebef6fdf5919a875fb63ee` |
| PASS | bond waiting to unbond no longer counts as trust |  |  |
| PASS | they cannot withdraw before the delay | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · the unbonding delay has not elapsed yet) | `0x7b06a3ce61fb18dd91c8f4722a8532c3f03f0c526e3edc4186455453dcc55b26` |
| INFO | waiting 65s | the unbonding delay passes |  |
| PASS | they withdraw the unbonded amount | seller2 → bond.withdraw_unbonded | `0x6f1b6f4abfbf5900c52a7b96cfb0bace21581d5d4c88633822abb101a9cfb763` |
| PASS | the bond is empty | bonded 0 GEN |  |
| WARN | the seller receives the bond back | seller2 balance is 1.98 GEN, expected about 2 GEN (payout not finalized yet?) |  |
| PASS | after the bond exit: escrow holds at least what it owes | balance 0.000750 GEN ≥ owed 0 GEN + fees 0.00075 GEN |  |
| PASS | after the bond exit: bond contract holds at least the bonds | balance 0.04 GEN ≥ bonded 0.04 GEN |  |
| PASS | after the bond exit: the bench never holds value | balance 1 wei (only 1 wei of refused-call dust allowed) |  |
| PASS | after the bond exit: balances equal the books exactly | escrow 0.000750 GEN, bond contract 0.04 GEN |  |

## S8 — Final books: solvency and fee sweep

| | Step | Detail | Tx |
|---|---|---|---|
| PASS | nothing is owed once every deal is finished and every balance withdrawn (or only a pending deal remains) | owed 0 GEN, deals 6, released 3, refunded 2, settled 1 |  |
| INFO | platform fees accrued | 0.00075 GEN |  |
| PASS | the owner cannot withdraw more than the accrued fees | refused as expected (status=ACCEPTED exec=ERROR consensus=MAJORITY_AGREE result=rollback · amount must be 1..750000000000000 wei (currently withdrawable)) | `0x73176b09c400bfabf8b11fa3516b7318c1b7ea292dd3e0bf16de48a5df96dfc5` |
| PASS | the owner withdraws the accrued fees | owner → escrow.withdraw_fees | `0x76bfff4caa58f39f0ccdd1cc91a0118b8425c15482bc997968ef95eda28e124e` |
| WARN | the owner receives the fees | owner balance is 0.999999 GEN, expected about 1.000749 GEN (payout not finalized yet?) |  |
| PASS | final books: escrow holds at least what it owes | balance 0.000000 GEN ≥ owed 0 GEN + fees 0 GEN |  |
| PASS | final books: bond contract holds at least the bonds | balance 0.04 GEN ≥ bonded 0.04 GEN |  |
| PASS | final books: the bench never holds value | balance 1 wei (only 1 wei of refused-call dust allowed) |  |
| PASS | final books: balances equal the books exactly | escrow 0.000000 GEN, bond contract 0.04 GEN |  |
| INFO | bench statistics | opened 2, ruled 1, appealed 0, withdrawn 1 |  |
